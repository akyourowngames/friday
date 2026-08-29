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
	withSettings,
	withRecentSessions,
	withLastSessionId,
	bumpRecentSession,
} from "./config.ts";
export type { FridConfig, ProviderConfig, FridSettings } from "./config.ts";

// Settings store
export {
	SettingsStore,
	registerSetting,
	getSettingSchema,
	listSettings,
	settingsToJson,
	validateValue,
} from "./settings.ts";
export type { SettingSchema, SettingType, SettingValue } from "./settings.ts";

// Slash commands
export {
	registerSlashCommand,
	getSlashCommand,
	listSlashCommands,
	findCommands,
	clearSlashCommands,
	parseSlashCommand,
} from "./slash-commands.ts";
export type {
	SlashCommand,
	SlashCommandContext,
	SlashCommandResult,
	SlashCommandHost,
} from "./slash-commands.ts";

// Built-in slash commands
export { registerBuiltinCommands } from "./commands/builtin.ts";
export type { UsageTotals } from "./commands/builtin.ts";

// Sessions
export {
	createSession,
	loadSession,
	listSessions,
	deleteSession,
	recordMessage,
	updateMeta,
	readMeta,
	readMessages,
	appendMessage,
	writeMeta,
	newSessionMeta,
	getSessionsDirPath,
} from "./sessions.ts";
export type { SessionMeta, SavedSession } from "./sessions.ts";

// Compaction
export {
	compactTranscript,
	estimateMessageTokens,
	estimateTranscriptTokens,
	makeTransformContext,
	makeSummaryMessage,
} from "./compaction.ts";
export type { CompactOptions } from "./compaction.ts";

// Tools
export {
	bashTool,
	readTool,
	writeTool,
	editTool,
	globTool,
	grepTool,
	builtinTools,
} from "./tools/shell.ts";
export { websearchTool, formatSearchResults } from "./tools/websearch.ts";
export { isPathInside, resolveSafePath, tryStat } from "./tools/path-safety.ts";

// Permissions
export {
	DEFAULT_POLICY,
	decide,
	matchPattern,
	PermissionCache,
} from "./permissions.ts";
export type { PermissionMode, PermissionPolicy, PermissionRule, PermissionRequest } from "./permissions.ts";

// Retry
export { isRetryable, backoffDelay, retryAfterMs, withRetry } from "./retry.ts";
export type { RetryOptions } from "./retry.ts";

// Lifecycle hooks
export { HookRegistry } from "./hooks.ts";
export type {
	HookEvent,
	Hook,
	HookResult,
	HookPayloads,
	PreToolUsePayload,
	PostToolUsePayload,
	PreUserMessagePayload,
	PostAssistantMessagePayload,
	PreModelCallPayload,
	PostModelCallPayload,
	TurnEndPayload,
} from "./hooks.ts";

// Extension loader
export {
	buildHost,
	defaultExtensionsDir,
	discoverExtensions,
	importExtension,
	loadExtensions,
	runExtension,
} from "./extension-loader.ts";
export type { ExtensionHost, ExtensionModule, LoadResult, BuildHostOptions } from "./extension-loader.ts";

// Markdown
export { renderMarkdown, renderMarkdownColored, markdownToPlain } from "./markdown.ts";
export type { MarkdownLine, MarkdownSpan, ColoredLine, RenderColoredOptions } from "./markdown.ts";

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
