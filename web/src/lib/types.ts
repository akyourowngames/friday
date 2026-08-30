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
}

export interface ChatMessage {
	id: string;
	role: "user" | "assistant";
	text: string;
	status: "streaming" | "done";
	tools: ToolRun[];
	timestamp?: number;
}

export type { AgentMessage as WireMessage } from "@/src/types";

