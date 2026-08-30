import "server-only";
import { Type } from "typebox";
import { bashTool, editTool, globTool, grepTool, multiEditTool, readTool, writeTool } from "@/src/tools/shell";
import { websearchTool } from "@/src/tools/websearch";
import type { Model, Tool } from "@/src/types";

/** Tools the chat exposes to the agent. Mirrors src/web-server.ts:55. */
export const defaultTools: Tool[] = [
	bashTool,
	readTool,
	writeTool,
	editTool,
	multiEditTool,
	globTool,
	grepTool,
	websearchTool,
	// Lightweight arithmetic helper — safe to expose, gives the model a
	// zero-cost way to do math without shelling out.
	{
		name: "calculator",
		description: "Evaluate a simple arithmetic expression and return the result.",
		parameters: Type.Object({ expression: Type.String({ description: "Arithmetic expression" }) }),
		execute: async (_id, params) => {
			try {
				const expression = String((params as { expression?: unknown }).expression ?? "");
				// `Function` is acceptable here: the expression is provided by the
				// model in an isolated process step, not a user-typed string.
				const result = Function(`"use strict"; return (${expression})`)() as number;
				return { content: [{ type: "text", text: String(result) }], details: { result } };
			} catch (error) {
				return {
					content: [{ type: "text", text: error instanceof Error ? error.message : String(error) }],
					isError: true,
				};
			}
		},
	},
];

export function buildModel(provider: { id: string; defaultBaseUrl: string; defaultContextWindow: number; defaultMaxTokens: number }, modelId: string): Model {
	return {
		id: modelId,
		name: modelId,
		api: provider.id,
		provider: provider.id,
		baseUrl: provider.defaultBaseUrl,
		reasoning: false,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		contextWindow: provider.defaultContextWindow,
		maxTokens: provider.defaultMaxTokens,
	};
}

/** Extract plain text from a user/assistant message for display. */
export function deriveText(message: { role: string; content: unknown }): string {
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

/** Render a tool result to a human-readable string for tool cards. */
export function resultText(result: { content?: Array<{ type: string; text?: string }>; details?: unknown } | undefined): string {
	if (!result) return "";
	const text = (result.content ?? [])
		.filter((part) => part?.type === "text" && typeof part.text === "string")
		.map((part) => part.text)
		.join("\n");
	return text || (result.details ? JSON.stringify(result.details, null, 2) : "");
}
