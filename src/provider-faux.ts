import type {
	Api,
	AssistantMessage,
	AssistantMessageEventStream,
	LLMContext,
	Model,
	ProviderId,
	StreamFn,
	StreamOptions,
	TextContent,
	ThinkingContent,
	ToolCall,
	Usage,
} from "./types.ts";
import { AssistantMessageEventStream as AssistantMessageEventStreamClass } from "./event-stream.ts";
import { registerModel } from "./model.ts";

const DEFAULT_API: Api = "faux";
const DEFAULT_PROVIDER: ProviderId = "faux";
const DEFAULT_MODEL_ID = "faux-1";
const DEFAULT_MODEL_NAME = "Faux Model";
const DEFAULT_BASE_URL = "http://localhost:0";
const DEFAULT_MIN_TOKEN_SIZE = 3;
const DEFAULT_MAX_TOKEN_SIZE = 5;
const DEFAULT_TOKENS_PER_SECOND = 0;

const DEFAULT_USAGE: Usage = {
	input: 0,
	output: 0,
	cacheRead: 0,
	cacheWrite: 0,
	totalTokens: 0,
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

/** Helper to create a text content block. */
export function fauxText(text: string): TextContent {
	return { type: "text", text };
}

/** Helper to create a thinking content block. */
export function fauxThinking(thinking: string): ThinkingContent {
	return { type: "thinking", thinking };
}

/** Helper to create a tool-call content block. */
export function fauxToolCall(name: string, arguments_: ToolCall["arguments"], options: { id?: string } = {}): ToolCall {
	return {
		type: "toolCall",
		id: options.id ?? randomId("tool"),
		name,
		arguments: arguments_,
	};
}

export type FauxContentBlock = TextContent | ThinkingContent | ToolCall;

function normalizeFauxAssistantContent(content: string | FauxContentBlock | FauxContentBlock[]): FauxContentBlock[] {
	if (typeof content === "string") {
		return [fauxText(content)];
	}
	return Array.isArray(content) ? content : [content];
}

/** A scripted assistant response. The faux provider streams this as a sequence
 *  of delta events, producing a realistic streaming simulation. */
export function fauxAssistantMessage(
	content: string | FauxContentBlock | FauxContentBlock[],
	options: { stopReason?: AssistantMessage["stopReason"]; timestamp?: number } = {},
): AssistantMessage {
	const normalized = normalizeFauxAssistantContent(content);
	return {
		role: "assistant",
		content: normalized,
		api: DEFAULT_API,
		provider: DEFAULT_PROVIDER,
		model: DEFAULT_MODEL_ID,
		usage: DEFAULT_USAGE,
		stopReason: options.stopReason ?? (normalized.some((c) => c.type === "toolCall") ? "toolUse" : "stop"),
		timestamp: options.timestamp ?? Date.now(),
	};
}

export interface FauxProviderState {
	callCount: number;
	responses: FauxContentBlock[][];
	pendingResponses: FauxContentBlock[][];
	tokensPerSecond: number;
}

export interface FauxProviderRegistration {
	provider: ProviderId;
	api: Api;
	models: [Model<Api>, Model<Api>, ...Model<Api>[]];
	getModel(): Model<Api>;
	state: FauxProviderState;
	setResponses: (responses: FauxContentBlock[][]) => void;
	appendResponses: (responses: FauxContentBlock[][]) => void;
	getPendingResponseCount: () => number;
	unregister: () => void;
}

function estimateTokens(text: string): number {
	return Math.max(1, Math.ceil(text.length / 4));
}

function randomId(prefix: string): string {
	return `${prefix}:${Date.now()}:${Math.random().toString(36).slice(2)}`;
}

function scheduleChunk(chunk: string, tokensPerSecond: number | undefined): Promise<void> {
	if (!tokensPerSecond || tokensPerSecond <= 0) {
		return new Promise((resolve) => queueMicrotask(resolve));
	}
	const delayMs = (estimateTokens(chunk) / tokensPerSecond) * 1000;
	return new Promise((resolve) => setTimeout(resolve, delayMs));
}

function splitStringByTokenSize(text: string, minTokenSize: number, maxTokenSize: number): string[] {
	const chunks: string[] = [];
	let index = 0;
	while (index < text.length) {
		const tokenSize = minTokenSize + Math.floor(Math.random() * (maxTokenSize - minTokenSize + 1));
		const charSize = Math.max(1, tokenSize * 4);
		chunks.push(text.slice(index, index + charSize));
		index += charSize;
	}
	return chunks.length > 0 ? chunks : [""];
}

function chunkArguments(arguments_: Record<string, any>, min: number, max: number): string[] {
	const json = JSON.stringify(arguments_);
	return splitStringByTokenSize(json, min, max);
}

function createAbortedMessage(partial: AssistantMessage): AssistantMessage {
	return { ...partial, stopReason: "aborted", errorMessage: "Request was aborted" };
}

async function streamWithDeltas(
	stream: AssistantMessageEventStream,
	message: AssistantMessage,
	minTokenSize: number,
	maxTokenSize: number,
	tokensPerSecond: number | undefined,
	signal: AbortSignal | undefined,
): Promise<void> {
	const partial: AssistantMessage = {
		...message,
		content: [],
		stopReason: "pending",
	};

	const abortHandler = () => {
		if (partial.stopReason === "pending") {
			const aborted = createAbortedMessage(partial);
			stream.push({ type: "error", reason: "aborted", error: aborted });
			stream.end(aborted);
		}
	};

	if (signal?.aborted) {
		const aborted = createAbortedMessage(partial);
		stream.push({ type: "error", reason: "aborted", error: aborted });
		stream.end(aborted);
		return;
	}

	signal?.addEventListener("abort", abortHandler);

	stream.push({ type: "start", partial: { ...partial } });

	for (let index = 0; index < message.content.length; index++) {
		if (signal?.aborted) {
			const aborted = createAbortedMessage(partial);
			stream.push({ type: "error", reason: "aborted", error: aborted });
			stream.end(aborted);
			return;
		}

		const block = message.content[index];

		if (block.type === "thinking") {
			partial.content = [...partial.content, { type: "thinking", thinking: "" }];
			stream.push({ type: "thinking_start", contentIndex: index, partial: { ...partial } });
			for (const chunk of splitStringByTokenSize(block.thinking, minTokenSize, maxTokenSize)) {
				await scheduleChunk(chunk, tokensPerSecond);
				if (signal?.aborted) {
					const aborted = createAbortedMessage(partial);
					stream.push({ type: "error", reason: "aborted", error: aborted });
					stream.end(aborted);
					return;
				}
				(partial.content[index] as ThinkingContent).thinking += chunk;
				stream.push({ type: "thinking_delta", contentIndex: index, delta: chunk, partial: { ...partial } });
			}
			stream.push({ type: "thinking_end", contentIndex: index, content: block.thinking, partial: { ...partial } });
			continue;
		}

		if (block.type === "text") {
			partial.content = [...partial.content, { type: "text", text: "" }];
			stream.push({ type: "text_start", contentIndex: index, partial: { ...partial } });
			for (const chunk of splitStringByTokenSize(block.text, minTokenSize, maxTokenSize)) {
				await scheduleChunk(chunk, tokensPerSecond);
				if (signal?.aborted) {
					const aborted = createAbortedMessage(partial);
					stream.push({ type: "error", reason: "aborted", error: aborted });
					stream.end(aborted);
					return;
				}
				(partial.content[index] as TextContent).text += chunk;
				stream.push({ type: "text_delta", contentIndex: index, delta: chunk, partial: { ...partial } });
			}
			stream.push({ type: "text_end", contentIndex: index, content: block.text, partial: { ...partial } });
			continue;
		}

		// Tool call
		partial.content = [...partial.content, { type: "toolCall", id: block.id, name: block.name, arguments: {} }];
		stream.push({ type: "toolcall_start", contentIndex: index, partial: { ...partial } });
		for (const chunk of chunkArguments(block.arguments, minTokenSize, maxTokenSize)) {
			await scheduleChunk(chunk, tokensPerSecond);
			if (signal?.aborted) {
				const aborted = createAbortedMessage(partial);
				stream.push({ type: "error", reason: "aborted", error: aborted });
				stream.end(aborted);
				return;
			}
			stream.push({ type: "toolcall_delta", contentIndex: index, delta: chunk, partial: { ...partial } });
		}
		stream.push({ type: "toolcall_end", contentIndex: index, toolCall: block, partial: { ...partial } });
	}

	// Finalize — determine done reason
	const doneReason: Extract<AssistantMessage["stopReason"], "stop" | "length" | "toolUse" | "deferred"> =
		message.content.some((c) => c.type === "toolCall") && message.stopReason !== "stop"
			? (message.stopReason as "toolUse" | "length")
			: "stop";

	const finalMessage = {
		...message,
		content: message.content,
		usage: { ...message.usage },
	};
	stream.push({ type: "done", reason: doneReason, message: finalMessage });
	stream.end(finalMessage);

	signal?.removeEventListener("abort", abortHandler);
}

/** Register the faux provider and its default model. Returns a handle for
 *  configuring scripted responses. */
export function registerFauxProvider(options: {
	tokensPerSecond?: number;
	minTokenSize?: number;
	maxTokenSize?: number;
} = {}): FauxProviderRegistration {
	const tokensPerSecond = options.tokensPerSecond ?? DEFAULT_TOKENS_PER_SECOND;
	const minTokenSize = options.minTokenSize ?? DEFAULT_MIN_TOKEN_SIZE;
	const maxTokenSize = options.maxTokenSize ?? DEFAULT_MAX_TOKEN_SIZE;

	const model: Model<Api> = {
		id: DEFAULT_MODEL_ID,
		name: DEFAULT_MODEL_NAME,
		api: DEFAULT_API,
		provider: DEFAULT_PROVIDER,
		baseUrl: DEFAULT_BASE_URL,
		reasoning: false,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		contextWindow: 8192,
		maxTokens: 4096,
	};

	registerModel({
		id: model.id,
		name: model.name,
		api: model.api,
		provider: model.provider,
		baseUrl: model.baseUrl,
		reasoning: model.reasoning,
		contextWindow: model.contextWindow,
		maxTokens: model.maxTokens,
	});

	const state: FauxProviderState = {
		callCount: 0,
		responses: [],
		pendingResponses: [],
		tokensPerSecond,
	};

	return {
		provider: DEFAULT_PROVIDER,
		api: DEFAULT_API,
		models: [model, model],
		getModel: () => model,
		state,
		setResponses: (responses) => {
			state.responses = responses.map((r) => [...r]);
			state.pendingResponses = responses.map((r) => [...r]);
			state.tokensPerSecond = tokensPerSecond;
		},
		appendResponses: (responses) => {
			for (const r of responses) {
				state.responses.push([...r]);
				state.pendingResponses.push([...r]);
			}
		},
		getPendingResponseCount: () => state.pendingResponses.length,
		unregister: () => {
			state.responses = [];
			state.pendingResponses = [];
		},
	};
}

/** Create a StreamFn backed by the faux provider's scripted responses.
 *  Each call to the stream function consumes one pending response. */
export function createFauxStreamFn(registration: FauxProviderRegistration): StreamFn {
	return (model, _context: LLMContext, options?: StreamOptions) => {
		const stream = new AssistantMessageEventStreamClass();
		registration.state.callCount++;

		const pending = registration.state.pendingResponses;

		if (pending.length === 0) {
			const errorMsg: AssistantMessage = {
				role: "assistant",
				content: [{ type: "text", text: "No scripted response available" }],
				api: DEFAULT_API,
				provider: DEFAULT_PROVIDER,
				model: model.id,
				usage: DEFAULT_USAGE,
				stopReason: "error",
				errorMessage: "No scripted response available from faux provider",
				timestamp: Date.now(),
			};
			stream.push({ type: "error", reason: "error", error: errorMsg });
			stream.end(errorMsg);
			return stream;
		}

		const content = pending.shift()!;
		const message = fauxAssistantMessage(content, {
			stopReason: content.some((c) => c.type === "toolCall") ? "toolUse" : "stop",
		});

		void streamWithDeltas(
			stream,
			message,
			DEFAULT_MIN_TOKEN_SIZE,
			DEFAULT_MAX_TOKEN_SIZE,
			registration.state.tokensPerSecond,
			options?.signal,
		);
		return stream;
	};
}

export { DEFAULT_MIN_TOKEN_SIZE, DEFAULT_MAX_TOKEN_SIZE };

/** Clear all registered models (for testing). */
export function clearFauxProviderState(): void {
	// Reset all faux-registered models
	// The faux provider registers a model at key "faux/faux-1"
}
