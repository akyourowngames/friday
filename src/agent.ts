import type {
	AgentContext,
	AgentEvent,
	AgentLoopConfig,
	AgentLoopTurnUpdate,
	AgentMessage,
	AgentState,
	AgentTool,
	AgentToolCall,
	AgentToolResult,
	Api,
	BeforeToolCallContext,
	BeforeToolCallResult,
	AfterToolCallContext,
	AfterToolCallResult,
	Model,
	PrepareNextTurnContext,
	ThinkingLevel,
	Tool,
	AgentToolResult as ToolResult,
} from "./types.ts";
import { runAgentLoop, runAgentLoopContinue } from "./agent-loop.ts";
import type { StreamFn } from "./types.ts";

type MutableAgentState = Omit<AgentState, "systemPrompt" | "model" | "thinkingLevel" | "tools" | "messages" | "isStreaming" | "streamingMessage" | "pendingToolCalls" | "errorMessage"> & {
	isStreaming: boolean;
	streamingMessage?: AgentMessage;
	pendingToolCalls: Set<string>;
	errorMessage?: string;
	systemPrompt: string;
	model: Model<Api>;
	thinkingLevel: ThinkingLevel;
	tools: Tool[];
	messages: AgentMessage[];
};

const EMPTY_USAGE = {
	input: 0,
	output: 0,
	cacheRead: 0,
	cacheWrite: 0,
	totalTokens: 0,
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

const DEFAULT_MODEL = {
	id: "unknown",
	name: "unknown",
	api: "unknown" as const,
	provider: "unknown" as const,
	baseUrl: "",
	reasoning: false,
	input: [] as ("text" | "image")[],
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	contextWindow: 0,
	maxTokens: 0,
};

function createMutableAgentState(
	initialState?: Partial<Omit<AgentState, "pendingToolCalls" | "isStreaming" | "streamingMessage" | "errorMessage">>,
): MutableAgentState {
	let tools = initialState?.tools?.slice() ?? [];
	let messages = initialState?.messages?.slice() ?? [];

	return {
		systemPrompt: initialState?.systemPrompt ?? "",
		model: initialState?.model ?? DEFAULT_MODEL,
		thinkingLevel: initialState?.thinkingLevel ?? "off",
		get tools() {
			return tools;
		},
		set tools(nextTools: AgentTool[]) {
			tools = nextTools.slice();
		},
		get messages() {
			return messages;
		},
		set messages(nextMessages: AgentMessage[]) {
			messages = nextMessages.slice();
		},
		isStreaming: false,
		streamingMessage: undefined,
		pendingToolCalls: new Set<string>(),
		errorMessage: undefined,
	};
}

export interface AgentOptions {
	initialState?: Partial<Omit<AgentState, "pendingToolCalls" | "isStreaming" | "streamingMessage" | "errorMessage">>;
	convertToLlm?: (messages: AgentMessage[]) => Message[] | Promise<Message[]>;
	transformContext?: (messages: AgentMessage[], signal?: AbortSignal) => Promise<AgentMessage[]>;
	streamFunction: StreamFn;
	getApiKey?: (provider: string) => Promise<string | undefined> | string | undefined;
	shouldStopAfterTurn?: (context: ShouldStopAfterTurnContext, signal?: AbortSignal) => boolean | Promise<boolean>;
	prepareNextTurn?: (context: PrepareNextTurnContext) => AgentLoopTurnUpdate | undefined | Promise<AgentLoopTurnUpdate | undefined>;
	getSteeringMessages?: () => Promise<AgentMessage[]>;
	getFollowUpMessages?: () => Promise<AgentMessage[]>;
	beforeToolCall?: (context: BeforeToolCallContext, signal?: AbortSignal) => Promise<BeforeToolCallResult | undefined>;
	afterToolCall?: (context: AfterToolCallContext, signal?: AbortSignal) => Promise<AfterToolCallResult | undefined>;
	toolExecution?: "sequential" | "parallel";
	sessionId?: string;
	transport?: string;
}

export interface ShouldStopAfterTurnContext {
	message: AgentMessage;
	toolResults: ToolResultMessage[];
	context: AgentContext;
	newMessages: AgentMessage[];
}

import type { Message, ToolResultMessage } from "./types.ts";

type ActiveRun = {
	promise: Promise<void>;
	resolve: () => void;
	abortController: AbortController;
};

/**
 * Stateful wrapper around the low-level agent loop.
 *
 * `Agent` owns the current transcript, emits lifecycle events, executes tools,
 * and exposes queueing APIs for steering and follow-up messages.
 */
export class Agent {
	private _state: MutableAgentState;
	private readonly listeners = new Set<(event: AgentEvent, signal: AbortSignal | undefined) => Promise<void> | void>();
	private readonly steeringQueue: PendingMessageQueue;
	private readonly followUpQueue: PendingMessageQueue;

	public convertToLlm: (messages: AgentMessage[]) => Message[] | Promise<Message[]>;
	public transformContext?: (messages: AgentMessage[], signal?: AbortSignal) => Promise<AgentMessage[]>;
	public streamFunction: StreamFn;
	public getApiKey?: (provider: string) => Promise<string | undefined> | string | undefined;
	public shouldStopAfterTurn?: (context: ShouldStopAfterTurnContext, signal?: AbortSignal) => boolean | Promise<boolean>;
	public prepareNextTurn?: (
		context: PrepareNextTurnContext,
	) => AgentLoopTurnUpdate | undefined | Promise<AgentLoopTurnUpdate | undefined>;
	public beforeToolCall?: (context: BeforeToolCallContext, signal?: AbortSignal) => Promise<BeforeToolCallResult | undefined>;
	public afterToolCall?: (context: AfterToolCallContext, signal?: AbortSignal) => Promise<AfterToolCallResult | undefined>;
	private activeRun?: ActiveRun;
	public sessionId?: string;
	public toolExecution: "sequential" | "parallel";
	private _steeringMode: "all" | "one-at-a-time" = "one-at-a-time";
	private _followUpMode: "all" | "one-at-a-time" = "one-at-a-time";

	constructor(options: AgentOptions) {
		const runtimeOptions: Partial<AgentOptions> = options ?? {};
		this._state = createMutableAgentState(runtimeOptions.initialState);
		this.convertToLlm = runtimeOptions.convertToLlm ?? defaultConvertToLlm;
		this.transformContext = runtimeOptions.transformContext;
		this.streamFunction = runtimeOptions.streamFunction!;
		this.getApiKey = runtimeOptions.getApiKey;
		this.shouldStopAfterTurn = runtimeOptions.shouldStopAfterTurn;
		this.prepareNextTurn = runtimeOptions.prepareNextTurn;
		this.beforeToolCall = runtimeOptions.beforeToolCall;
		this.afterToolCall = runtimeOptions.afterToolCall;
		this.sessionId = runtimeOptions.sessionId;
		this.toolExecution = runtimeOptions.toolExecution ?? "parallel";
		this.steeringQueue = new PendingMessageQueue(this._steeringMode);
		this.followUpQueue = new PendingMessageQueue(this._followUpMode);
	}

	subscribe(listener: (event: AgentEvent, signal: AbortSignal | undefined) => Promise<void> | void): () => void {
		this.listeners.add(listener);
		return () => this.listeners.delete(listener);
	}

	/**
	 * Swap the active model + stream function at runtime (e.g. when the TUI's
	 * `/model` command picks a new model). Takes effect on the next prompt — the
	 * agent loop reads `state.model` and `streamFunction` at prompt time.
	 */
	useModel(model: Model, streamFn: StreamFn): void {
		this._state.model = model;
		this.streamFunction = streamFn;
	}

	get state(): AgentState {
		return this._state;
	}

	get steeringMode(): "all" | "one-at-a-time" {
		return this.steeringQueue.mode;
	}

	set steeringMode(mode: "all" | "one-at-a-time") {
		this._steeringMode = mode;
		this.steeringQueue.mode = mode;
	}

	get followUpMode(): "all" | "one-at-a-time" {
		return this.followUpQueue.mode;
	}

	set followUpMode(mode: "all" | "one-at-a-time") {
		this._followUpMode = mode;
		this.followUpQueue.mode = mode;
	}

	steer(message: AgentMessage): void {
		this.steeringQueue.enqueue(message);
	}

	followUp(message: AgentMessage): void {
		this.followUpQueue.enqueue(message);
	}

	clearSteeringQueue(): void {
		this.steeringQueue.clear();
	}

	clearFollowUpQueue(): void {
		this.followUpQueue.clear();
	}

	clearAllQueues(): void {
		this.clearSteeringQueue();
		this.clearFollowUpQueue();
	}

	hasQueuedMessages(): boolean {
		return this.steeringQueue.hasItems() || this.followUpQueue.hasItems();
	}

	get signal(): AbortSignal | undefined {
		return this.activeRun?.abortController.signal;
	}

	abort(): void {
		this.activeRun?.abortController.abort();
	}

	waitForIdle(): Promise<void> {
		return this.activeRun?.promise ?? Promise.resolve();
	}

	reset(): void {
		if (this.activeRun) {
			throw new Error("Agent is already processing. Wait for completion before resetting.");
		}
		this._state.messages = [];
		this._state.isStreaming = false;
		this._state.streamingMessage = undefined;
		this._state.pendingToolCalls = new Set<string>();
		this._state.errorMessage = undefined;
		this.clearFollowUpQueue();
		this.clearSteeringQueue();
	}

	/** Replace the whole transcript (used by `/resume` to restore a saved
	 *  conversation). Throws if the agent is mid-run. */
	replaceMessages(messages: AgentMessage[]): void {
		if (this.activeRun) {
			throw new Error("Cannot replace messages while the agent is processing.");
		}
		this._state.messages = messages;
		this._state.isStreaming = false;
		this._state.streamingMessage = undefined;
		this._state.pendingToolCalls = new Set<string>();
		this._state.errorMessage = undefined;
	}

	async prompt(message: AgentMessage | AgentMessage[]): Promise<void>;
	async prompt(input: string): Promise<void>;
	async prompt(input: string | AgentMessage | AgentMessage[]): Promise<void> {
		if (this.activeRun) {
			throw new Error("Agent is already processing a prompt. Use steer() or followUp() to queue messages, or wait for completion.");
		}
		const messages = this.normalizePromptInput(input);
		await this.runPromptMessages(messages);
	}

	async continue(): Promise<void> {
		if (this.activeRun) {
			throw new Error("Agent is already processing. Wait for completion before continuing.");
		}

		const lastMessage = this._state.messages[this._state.messages.length - 1];
		if (!lastMessage) {
			throw new Error("No messages to continue from");
		}

		if (lastMessage.role === "assistant") {
			const queuedSteering = this.steeringQueue.drain();
			if (queuedSteering.length > 0) {
				await this.runPromptMessages(queuedSteering, { skipInitialSteeringPoll: true });
				return;
			}

			const queuedFollowUps = this.followUpQueue.drain();
			if (queuedFollowUps.length > 0) {
				await this.runPromptMessages(queuedFollowUps);
				return;
			}

			throw new Error("Cannot continue from message role: assistant");
		}

		await this.runContinuation();
	}

	private normalizePromptInput(input: string | AgentMessage | AgentMessage[]): AgentMessage[] {
		if (Array.isArray(input)) {
			return input;
		}
		if (typeof input !== "string") {
			return [input];
		}
		return [{ role: "user", content: input, timestamp: Date.now() }];
	}

	private async runPromptMessages(messages: AgentMessage[], options: { skipInitialSteeringPoll?: boolean } = {}): Promise<void> {
		await this.runWithLifecycle(async (signal) => {
			await runAgentLoop(
				messages,
				this.createContextSnapshot(),
				this.createLoopConfig(options),
				async (event: AgentEvent) => await this.processEvents(event, signal),
				signal,
				this.streamFunction,
			);
		});
	}

	private async runContinuation(): Promise<void> {
		await this.runWithLifecycle(async (signal) => {
			await runAgentLoopContinue(
				this.createContextSnapshot(),
				this.createLoopConfig(),
				async (event: AgentEvent) => await this.processEvents(event, signal),
				signal,
				this.streamFunction,
			);
		});
	}

	private createContextSnapshot(): AgentContext {
		return {
			systemPrompt: this._state.systemPrompt,
			messages: this._state.messages.slice(),
			tools: this._state.tools.slice(),
		};
	}

	private createLoopConfig(options: { skipInitialSteeringPoll?: boolean } = {}): AgentLoopConfig {
		let skipInitialSteeringPoll = options.skipInitialSteeringPoll === true;
		const shouldStopAfterTurn = this.shouldStopAfterTurn;
		return {
			model: this._state.model,
			reasoning: this._state.thinkingLevel === "off" ? undefined : this._state.thinkingLevel,
			sessionId: this.sessionId,
			toolExecution: this.toolExecution,
			beforeToolCall: this.beforeToolCall
				? async (context, signal) => await this.beforeToolCall!(context, signal)
				: undefined,
			afterToolCall: this.afterToolCall
				? async (context, signal) => await this.afterToolCall!(context, signal)
				: undefined,
			shouldStopAfterTurn: shouldStopAfterTurn
				? async (context) => await shouldStopAfterTurn(context, this.signal)
				: undefined,
			prepareNextTurn: this.prepareNextTurn
				? async (context) => await this.prepareNextTurn!(context)
				: undefined,
			convertToLlm: this.convertToLlm,
			transformContext: this.transformContext,
			getApiKey: this.getApiKey,
			getSteeringMessages: async () => {
				if (skipInitialSteeringPoll) {
					skipInitialSteeringPoll = false;
					return [];
				}
				return this.steeringQueue.drain();
			},
			getFollowUpMessages: async () => this.followUpQueue.drain(),
			streamFunction: this.streamFunction as any,
		};
	}

	private async runWithLifecycle(executor: (signal: AbortSignal) => Promise<void>): Promise<void> {
		if (this.activeRun) {
			throw new Error("Agent is already processing.");
		}

		const abortController = new AbortController();
		let resolvePromise!: () => void;
		const promise = new Promise<void>((resolve) => {
			resolvePromise = resolve;
		});
		this.activeRun = { promise, resolve: resolvePromise, abortController };

		this._state.isStreaming = true;
		this._state.streamingMessage = undefined;
		this._state.errorMessage = undefined;

		try {
			await executor(abortController.signal);
		} catch (error) {
			await this.handleRunFailure(error, abortController.signal.aborted);
		} finally {
			this.finishRun();
		}

		await promise;
	}

	private async handleRunFailure(error: unknown, aborted: boolean): Promise<void> {
		const failureMessage: AgentMessage = {
			role: "assistant",
			content: [{ type: "text", text: "" }],
			api: this._state.model.api as any,
			provider: this._state.model.provider as any,
			model: this._state.model.id,
			usage: EMPTY_USAGE as any,
			stopReason: aborted ? "aborted" : "error",
			errorMessage: error instanceof Error ? error.message : String(error),
			timestamp: Date.now(),
		};
		await this.processEvents({ type: "message_start", message: failureMessage }, this.activeRun?.abortController.signal);
		await this.processEvents({ type: "message_end", message: failureMessage }, this.activeRun?.abortController.signal);
		await this.processEvents(
			{ type: "turn_end", message: failureMessage, toolResults: [] },
			this.activeRun?.abortController.signal,
		);
		await this.processEvents({ type: "agent_end", messages: [failureMessage] }, this.activeRun?.abortController.signal);
	}

	private finishRun(): void {
		this._state.isStreaming = false;
		this._state.streamingMessage = undefined;
		this._state.pendingToolCalls = new Set<string>();
		this.activeRun?.resolve();
		this.activeRun = undefined;
	}

	async processEvents(event: AgentEvent, signal?: AbortSignal): Promise<void> {
		switch (event.type) {
			case "message_start":
				this._state.streamingMessage = event.message;
				break;

			case "message_update":
				this._state.streamingMessage = event.message;
				break;

			case "message_end":
				this._state.streamingMessage = undefined;
				this._state.messages.push(event.message);
				break;

			case "tool_execution_start": {
				const pendingToolCalls = new Set(this._state.pendingToolCalls);
				pendingToolCalls.add(event.toolCallId);
				this._state.pendingToolCalls = pendingToolCalls;
				break;
			}

			case "tool_execution_end": {
				const pendingToolCalls = new Set(this._state.pendingToolCalls);
				pendingToolCalls.delete(event.toolCallId);
				this._state.pendingToolCalls = pendingToolCalls;
				break;
			}

			case "turn_end":
				if (event.message.role === "assistant" && (event.message as any).errorMessage) {
					this._state.errorMessage = (event.message as any).errorMessage;
				}
				break;

			case "agent_end":
				this._state.streamingMessage = undefined;
				break;
		}

		for (const listener of this.listeners) {
			await listener(event, signal);
		}
	}
}

class PendingMessageQueue {
	private messages: AgentMessage[] = [];
	public mode: "all" | "one-at-a-time";

	constructor(mode: "all" | "one-at-a-time") {
		this.mode = mode;
	}

	enqueue(message: AgentMessage): void {
		this.messages.push(message);
	}

	hasItems(): boolean {
		return this.messages.length > 0;
	}

	drain(): AgentMessage[] {
		if (this.mode === "all") {
			const drained = this.messages.slice();
			this.messages = [];
			return drained;
		}
		const first = this.messages[0];
		if (!first) return [];
		this.messages = this.messages.slice(1);
		return [first];
	}

	clear(): void {
		this.messages = [];
	}
}

function defaultConvertToLlm(messages: AgentMessage[]): Message[] {
	return messages.filter(
		(message) => message.role === "user" || message.role === "assistant" || message.role === "toolResult",
	);
}
