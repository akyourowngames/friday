/**
 * Type re-exports + UI-friendly shapes.
 *
 * The client never imports server-only modules directly. These types
 * describe just enough of the wire protocol for the chat UI to render
 * messages, tool cards, and the various settings controls.
 */

export type {
	AgentEvent,
	AgentMessage,
	AssistantMessage,
	ImageContent,
	Message,
	StopReason,
	TextContent,
	ThinkingContent,
	ToolCall,
	ToolResult,
	ToolResultMessage,
	Usage,
	UserMessage,
} from "@/src/types";
export type { SettingValue } from "@/src/settings";
export type { SearchHit, ProviderSource } from "@/src/tools/websearch";

export interface ProviderInfo {
	id: string;
	name: string;
	defaultModel: string;
	requiresKey: boolean;
}

export interface SessionMeta {
	id: string;
	title: string;
	updatedAt: string;
	messageCount: number;
	provider?: string;
	model?: string;
}

export interface SettingSchema {
	key: string;
	label: string;
	description: string;
	type: string;
	defaultValue?: string | number | boolean | string[] | null;
	options?: readonly string[];
	min?: number;
	max?: number;
	group?: string;
	hintUrl?: string;
}

/** UI representation of a tool call currently in flight or just finished. */
export interface ToolRun {
	id: string;
	name: string;
	args: unknown;
	output: string;
	status: "running" | "done" | "error";
	category: "shell" | "file" | "search" | "other";
	expanded?: boolean;
	/** For websearch: the synthesized answer returned by the tool. */
	searchAnswer?: string;
	/** For websearch: the structured results so the UI can render citation cards. */
	searchResults?: Array<{ title: string; url: string; snippet: string }>;
	/** For websearch: per-provider results table, for the "Sources consulted" footer. */
	searchSources?: Array<{ provider: string; status: string; latencyMs?: number; hitCount?: number; error?: string }>;
	/** For websearch: provider id that produced the answer (if any). */
	searchProvider?: string;
}

export interface RunResult {
	command: string;
	cwd: string;
	platform: string;
	shell: string;
	stdout: string;
	stderr: string;
	exitCode: number | null;
	timedOut: boolean;
	aborted: boolean;
	durationMs: number;
}

export interface ChatMessage {
	id: string;
	role: "user" | "assistant" | "run";
	text: string;
	status: "streaming" | "done";
	tools: ToolRun[];
	timestamp?: number;
	/** Local Quick Run invocation (only set when role === "run"). */
	run?: RunResult;
}

export type { AgentMessage as WireMessage } from "@/src/types";

