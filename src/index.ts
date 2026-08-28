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

export { validateToolArguments, validateToolArgumentsOrThrow } from "./validate.ts";
export type { ValidationResult } from "./validate.ts";

export { registerModel, getModel, listModels, clearModels } from "./model.ts";

export {
	registerFauxProvider,
	createFauxStreamFn,
	fauxText,
	fauxThinking,
	fauxToolCall,
	fauxAssistantMessage,
	clearFauxProviderState,
} from "./provider-faux.ts";

// Universal OpenAI-compatible provider (OpenAI, Groq, OpenRouter, DeepSeek, Mistral, Together, Ollama)
export {
	createOpenAICompatStreamFn,
	listOpenAICompatModels,
} from "./providers/openai-compat.ts";

// Anthropic, Google, Ollama
export { createAnthropicStreamFn, listAnthropicModels } from "./providers/anthropic.ts";
export { createGoogleStreamFn, listGoogleModels } from "./providers/google.ts";
export { createOllamaStreamFn, listOllamaModels, isOllamaRunning } from "./providers/ollama.ts";

// Provider catalog
export {
	getProvider,
	findProvider,
	listProviders,
	resolveApiKey,
	resolveBaseUrl,
} from "./providers/registry.ts";
export type { ProviderMeta, ApiStyle } from "./providers/registry.ts";

// Config + interactive
export {
	loadConfig,
	saveConfig,
	getConfigFilePath,
	getApiKey,
	getBaseUrl,
	getLastModel,
	withApiKey,
	withBaseUrl,
	withLastModel,
	withLastProvider,
	resetConfig,
} from "./config.ts";
export type { FridConfig, ProviderConfig } from "./config.ts";

export { setupProvider, readSecret, readLine, pickModel, listModelsForProvider } from "./interactive.ts";
export type { SetupResult, SetupOptions } from "./interactive.ts";

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
