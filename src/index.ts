/**
 * friday-ng — Next-generation AI assistant with instant token streaming.
 *
 * Built on the Pi Agent Harness streaming architecture.
 */
export { Agent } from "./agent.ts";
export type { AgentOptions, ShouldStopAfterTurnContext } from "./agent.ts";

export {
	agentLoop,
	agentLoopContinue,
	runAgentLoop,
	runAgentLoopContinue,
} from "./agent-loop.ts";

export { EventStream, AssistantMessageEventStream, createAssistantMessageEventStream } from "./event-stream.ts";

export { validateToolArguments } from "./validate.ts";

export { registerModel, getModel, listModels, clearModels } from "./model.ts";

export {
	registerFauxProvider,
	createFauxStreamFn,
	fauxText,
	fauxThinking,
	fauxToolCall,
	fauxAssistantMessage,
} from "./provider-faux.ts";

export { setDefaultStreamFn, getDefaultStreamFn } from "./stream-fn.ts";

export { ConsoleRenderer, attachConsoleRenderer } from "./console-renderer.ts";

export type {
	Api,
	AssistantMessage,
	AssistantMessageEvent,
	AgentContext,
	AgentEvent,
	AgentEventSink,
	AgentLoopConfig,
	AgentLoopTurnUpdate,
	AgentMessage,
	AgentState,
	AgentTool,
	AgentToolCall,
	AgentToolResult,
	BeforeToolCallContext,
	BeforeToolCallResult,
	AfterToolCallContext,
	AfterToolCallResult,
	PrepareNextTurnContext,
	ImageContent,
	LLMContext,
	ThinkingContent,
	ThinkingLevel,
	Tool,
	ToolResult,
	ToolResultMessage,
	ToolCall,
	StreamFn,
	StreamOptions,
	StopReason,
	Usage,
	Message,
	Model,
	ProviderId,
} from "./types.ts";
