import type { Static, TSchema } from "typebox";
import type { AssistantMessageEventStream } from "./event-stream.ts";
export type { AssistantMessageEventStream } from "./event-stream.ts";

// --- Provider / Model types ---

export type KnownProvider = "faux" | "openai";
export type ProviderId = KnownProvider | (string & {});
export type KnownApi = "faux" | "openai";
export type Api = KnownApi | (string & {});

export interface ModelCost {
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
	total: number;
}

export interface Model<TPrimeApi extends Api = Api> {
	id: string;
	name: string;
	api: TPrimeApi;
	provider: ProviderId;
	baseUrl: string;
	reasoning: boolean;
	input: ("text" | "image")[];
	cost: ModelCost;
	contextWindow: number;
	maxTokens: number;
}

// --- Message content blocks ---

export interface TextContent {
	type: "text";
	text: string;
}

export interface ImageContent {
	type: "image";
	data: string;
	mimeType: string;
}

export interface ThinkingContent {
	type: "thinking";
	thinking: string;
}

export interface ToolCall {
	type: "toolCall";
	id: string;
	name: string;
	arguments: Record<string, any>;
}

// --- Messages ---

export type StopReason = "pending" | "stop" | "length" | "toolUse" | "error" | "aborted" | "deferred";

export interface Usage {
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
	reasoning?: number;
	totalTokens: number;
	cost: {
		input: number;
		output: number;
		cacheRead: number;
		cacheWrite: number;
		total: number;
	};
}

export interface UserMessage {
	role: "user";
	content: string | (TextContent | ImageContent)[];
	timestamp: number;
}

export interface AssistantMessage {
	role: "assistant";
	content: (TextContent | ThinkingContent | ToolCall)[];
	api: Api;
	provider: ProviderId;
	model: string;
	usage: Usage;
	stopReason: StopReason;
	errorMessage?: string;
	timestamp: number;
}

export interface ToolResultMessage {
	role: "toolResult";
	toolCallId: string;
	toolName: string;
	content: (TextContent | ImageContent)[];
	usage?: Usage;
	details?: unknown;
	isError: boolean;
	timestamp: number;
}

export type Message = UserMessage | AssistantMessage | ToolResultMessage;

// --- Streaming event protocol ---

export type AssistantMessageEvent =
	| { type: "start"; partial: AssistantMessage }
	| { type: "text_start"; contentIndex: number; partial: AssistantMessage }
	| { type: "text_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
	| { type: "text_end"; contentIndex: number; content: string; partial: AssistantMessage }
	| { type: "thinking_start"; contentIndex: number; partial: AssistantMessage }
	| { type: "thinking_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
	| { type: "thinking_end"; contentIndex: number; content: string; partial: AssistantMessage }
	| { type: "toolcall_start"; contentIndex: number; partial: AssistantMessage }
	| { type: "toolcall_delta"; contentIndex: number; delta: string; partial: AssistantMessage }
	| { type: "toolcall_end"; contentIndex: number; toolCall: ToolCall; partial: AssistantMessage }
	| { type: "done"; reason: Extract<StopReason, "stop" | "length" | "toolUse" | "deferred">; message: AssistantMessage }
	| { type: "error"; reason: Extract<StopReason, "aborted" | "error">; error: AssistantMessage };

// --- Tool definition ---

export interface Tool<TParameters extends TSchema = TSchema, TResult = any> extends ToolBase<TParameters> {
	prepareArguments?: (args: unknown) => Static<TParameters>;
	executionMode?: "sequential" | "parallel";
	execute: (
		toolCallId: string,
		params: Static<TParameters>,
		signal?: AbortSignal,
	) => Promise<ToolResult>;
}

export interface ToolBase<TParameters extends TSchema = TSchema> {
	name: string;
	description: string;
	parameters: TParameters;
}

export interface ToolResult {
	content: (TextContent | ImageContent)[];
	details?: unknown;
	/** Usage from the tool execution itself, if available. */
	usage?: Usage;
	/** Names of tools introduced by this result and available from this transcript point onward. */
	addedToolNames?: string[];
	/** Hint that the agent should stop after the current tool batch. */
	terminate?: boolean;
	/** Set by tools that want to surface an error result to the model without throwing. */
	isError?: boolean;
}

// --- Context (sent to LLM) ---

export interface LLMContext {
	systemPrompt?: string;
	messages: Message[];
	tools?: ToolBase[];
}

// --- Stream function ---

export type StreamOptions = {
	signal?: AbortSignal;
	apiKey?: string;
	sessionId?: string;
	temperature?: number;
	maxTokens?: number;
};

export type StreamFn = (
	model: Model<Api>,
	context: LLMContext,
	options?: StreamOptions,
) => AssistantMessageEventStream | Promise<AssistantMessageEventStream>;

// --- Agent messages (wraps LLM messages with room for app-specific types) ---

export type AgentMessage = Message;
export type AgentToolResult = ToolResult;
export type AgentToolCall = ToolCall;
export type AgentTool<TParameters extends TSchema = TSchema, TResult = any> = Tool<TParameters, TResult>;
export type ThinkingLevel = "off" | "minimal" | "low" | "medium" | "high";

// --- Agent context ---

export interface AgentContext {
	systemPrompt: string;
	messages: AgentMessage[];
	tools?: Tool[];
}

// --- Agent loop config ---

export interface AgentLoopConfig {
	model: Model<Api>;
	convertToLlm: (messages: AgentMessage[]) => Message[] | Promise<Message[]>;
	streamFunction: StreamFn;
	reasoning?: ThinkingLevel;
	sessionId?: string;
	transformContext?: (messages: AgentMessage[], signal?: AbortSignal) => Promise<AgentMessage[]>;
	getApiKey?: (provider: string) => Promise<string | undefined> | string | undefined;
	apiKey?: string;
	getSteeringMessages?: () => Promise<AgentMessage[]>;
	getFollowUpMessages?: () => Promise<AgentMessage[]>;
	shouldStopAfterTurn?: (context: ShouldStopAfterTurnContext) => boolean | Promise<boolean>;
	prepareNextTurn?: (
		context: PrepareNextTurnContext,
	) => Promise<AgentLoopTurnUpdate | undefined> | AgentLoopTurnUpdate | undefined;
	beforeToolCall?: (context: BeforeToolCallContext, signal?: AbortSignal) => Promise<BeforeToolCallResult | undefined>;
	afterToolCall?: (context: AfterToolCallContext, signal?: AbortSignal) => Promise<AfterToolCallResult | undefined>;
	toolExecution?: "sequential" | "parallel";
}

export interface ShouldStopAfterTurnContext {
	message: AgentMessage;
	toolResults: ToolResultMessage[];
	context: AgentContext;
	newMessages: AgentMessage[];
}

export interface PrepareNextTurnContext {
	message: AssistantMessage;
	toolResults: ToolResultMessage[];
	context: AgentContext;
	newMessages: AgentMessage[];
}

export interface AgentLoopTurnUpdate {
	context?: AgentContext;
	model?: Model<Api>;
	thinkingLevel?: ThinkingLevel;
}

export interface BeforeToolCallContext {
	assistantMessage: AssistantMessage;
	toolCall: ToolCall;
	args: unknown;
	context: AgentContext;
}

export interface BeforeToolCallResult {
	block?: boolean;
	reason?: string;
	terminate?: boolean;
}

export interface AfterToolCallContext {
	assistantMessage: AssistantMessage;
	toolCall: ToolCall;
	args: unknown;
	result: ToolResult;
	isError: boolean;
	context: AgentContext;
}

export interface AfterToolCallResult {
	content?: (TextContent | ImageContent)[];
	details?: unknown;
	usage?: Usage;
	addedToolNames?: string[];
	isError?: boolean;
	terminate?: boolean;
}

// --- Agent events ---

export type AgentEvent =
	| { type: "agent_start" }
	| { type: "agent_end"; messages: AgentMessage[] }
	| { type: "turn_start" }
	| { type: "turn_end"; message: AgentMessage; toolResults: ToolResultMessage[] }
	| { type: "message_start"; message: AgentMessage }
	| { type: "message_update"; message: AgentMessage; assistantMessageEvent: AssistantMessageEvent }
	| { type: "message_end"; message: AgentMessage }
	| { type: "tool_execution_start"; toolCallId: string; toolName: string; args: any }
	| { type: "tool_execution_end"; toolCallId: string; toolName: string; result: ToolResult; isError: boolean };

export type AgentEventSink = (event: AgentEvent) => Promise<void> | void;

// --- Agent state ---

export interface AgentState {
	readonly systemPrompt: string;
	readonly model: Model<Api>;
	readonly thinkingLevel: ThinkingLevel;
	readonly tools: Tool[];
	readonly messages: AgentMessage[];
	readonly isStreaming: boolean;
	readonly streamingMessage?: AgentMessage;
	readonly pendingToolCalls: ReadonlySet<string>;
	readonly errorMessage?: string;
}
