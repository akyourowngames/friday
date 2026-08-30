import type { AgentMessage } from "./types";

/** Pull plain text out of a user or assistant message for display. */
export function deriveText(message: AgentMessage): string {
	if (message.role === "user") {
		if (typeof message.content === "string") return message.content;
		if (Array.isArray(message.content)) {
			return message.content
				.filter((part): part is { type: "text"; text: string } => part?.type === "text")
				.map((part) => part.text)
				.join("");
		}
		return "";
	}
	if (message.role === "assistant" && Array.isArray(message.content)) {
		return message.content
			.filter((part): part is { type: "text"; text: string } => part?.type === "text")
			.map((part) => part.text)
			.join("");
	}
	return "";
}

/** Map a tool name to one of our 4 visual categories. */
export function toolCategory(name: string): "shell" | "file" | "search" | "other" {
	if (name === "bash" || name === "calculator") return "shell";
	if (name === "read" || name === "write" || name === "edit" || name === "multi_edit" || name === "glob" || name === "grep")
		return "file";
	if (name === "websearch") return "search";
	return "other";
}

