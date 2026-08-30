"use client";

import { useCallback, useRef, useState } from "react";
import { api } from "./api";
import { parseSse } from "./sse";
import type { AgentEvent, AgentMessage, ChatMessage, ProviderInfo, SettingSchema, ToolRun } from "./types";
import { deriveText, toolCategory } from "./types-helpers";

/**
 * useChatSession — owns the chat transcript, the active provider/model,
 * and the live SSE stream. Components consume its return value; nothing
 * else talks to /api/chat directly.
 */
export interface UseChatSessionOptions {
	initialProvider: string;
	initialModel: string;
	providers: ProviderInfo[];
	settings: Record<string, unknown>;
	settingSchema: SettingSchema[];
	sessionId: string | null;
	onSessionChange: (id: string | null) => void;
	onSettingsChange: (next: Record<string, unknown>) => void;
}

export interface UseChatSessionResult {
	messages: ChatMessage[];
	isStreaming: boolean;
	provider: string;
	model: string;
	sessionId: string | null;
	submit: (prompt: string) => Promise<void>;
	abort: () => Promise<void>;
	setProvider: (id: string) => void;
	setModel: (id: string) => void;
	loadTranscript: (id: string, transcript: AgentMessage[]) => void;
	toggleTool: (messageId: string, toolId: string) => void;
	resetSession: () => void;
	settings: Record<string, unknown>;
	settingSchema: SettingSchema[];
	saveSettings: (next: Record<string, unknown>) => Promise<void>;
}

export function useChatSession(opts: UseChatSessionOptions): UseChatSessionResult {
	const [messages, setMessages] = useState<ChatMessage[]>([]);
	const [isStreaming, setIsStreaming] = useState(false);
	const [provider, setProvider] = useState(opts.initialProvider);
	const [model, setModel] = useState(opts.initialModel);
	const [sessionId, setSessionId] = useState<string | null>(opts.sessionId);
	const [settings, setSettings] = useState<Record<string, unknown>>(opts.settings);

	const controllerRef = useRef<AbortController | null>(null);
	const assistantIdRef = useRef<string | null>(null);

	/** Apply an agent event to the transcript. Pure: takes a snapshot, returns a new one. */
	const applyEvent = useCallback((event: AgentEvent, assistantId: string) => {
		setMessages((current) => reduceEvent(current, event, assistantId));
	}, []);

	const submit = useCallback(
		async (text: string) => {
			const prompt = text.trim();
			if (!prompt || isStreaming) return;

			const userId = newId();
			const assistantId = newId();
			assistantIdRef.current = assistantId;

			setMessages((current) => [
				...current,
				{ id: userId, role: "user", text: prompt, status: "done", tools: [], timestamp: Date.now() },
				{ id: assistantId, role: "assistant", text: "", status: "streaming", tools: [] },
			]);
			setIsStreaming(true);

			const controller = new AbortController();
			controllerRef.current = controller;

			try {
				const response = await fetch("/api/chat", {
					method: "POST",
					headers: { "content-type": "application/json" },
					body: JSON.stringify({ prompt, sessionId: sessionId ?? undefined, provider, model }),
					signal: controller.signal,
				});
				if (!response.ok || !response.body) {
					throw new Error(`The harness did not open a stream (${response.status}).`);
				}

				for await (const frame of parseSse(response.body, controller.signal)) {
					if (frame.event === "session") {
						const payload = frame.data as { id: string };
						setSessionId(payload.id);
						opts.onSessionChange(payload.id);
					} else if (frame.event === "agent-event") {
						applyEvent(frame.data as AgentEvent, assistantId);
					} else if (frame.event === "error") {
						const payload = frame.data as { message: string };
						setMessages((current) =>
							current.map((m) => (m.id === assistantId ? { ...m, text: `Connection error: ${payload.message}`, status: "done" } : m)),
						);
					}
				}
			} catch (error) {
				if ((error as Error).name !== "AbortError") {
					setMessages((current) =>
						current.map((m) => (m.id === assistantId ? { ...m, text: `Connection error: ${(error as Error).message}`, status: "done" } : m)),
					);
				}
			} finally {
				controllerRef.current = null;
				assistantIdRef.current = null;
				setIsStreaming(false);
			}
		},
		[applyEvent, isStreaming, model, provider, sessionId, opts],
	);

	const abort = useCallback(async () => {
		controllerRef.current?.abort();
		if (sessionId) {
			try {
				await api.post<{ ok: boolean }>("/api/abort", { sessionId });
			} catch {
				// best effort
			}
		}
		setIsStreaming(false);
	}, [sessionId]);

	const loadTranscript = useCallback((id: string, transcript: AgentMessage[]) => {
		setSessionId(id);
		const next: ChatMessage[] = [];
		for (const message of transcript) {
			const text = deriveText(message);
			if (message.role === "user") {
				next.push({ id: `${id}-${message.timestamp ?? next.length}-u`, role: "user", text, status: "done", tools: [], timestamp: message.timestamp });
			} else if (message.role === "assistant") {
				next.push({ id: `${id}-${message.timestamp ?? next.length}-a`, role: "assistant", text, status: "done", tools: [], timestamp: message.timestamp });
			}
		}
		setMessages(next);
	}, []);

	const resetSession = useCallback(() => {
		setSessionId(null);
		setMessages([]);
		opts.onSessionChange(null);
	}, [opts]);

	const toggleTool = useCallback((messageId: string, toolId: string) => {
		setMessages((current) =>
			current.map((message) =>
				message.id !== messageId
					? message
					: { ...message, tools: message.tools.map((tool) => (tool.id === toolId ? { ...tool, expanded: !tool.expanded } : tool)) },
			),
		);
	}, []);

	const saveSettings = useCallback(
		async (next: Record<string, unknown>) => {
			setSettings(next);
			opts.onSettingsChange(next);
			try {
				await api.post<{ settings: Record<string, unknown> }>("/api/settings", { settings: next });
			} catch {
				// best effort
			}
		},
		[opts],
	);

	return {
		messages,
		isStreaming,
		provider,
		model,
		sessionId,
		submit,
		abort,
		setProvider,
		setModel,
		loadTranscript,
		toggleTool,
		resetSession,
		settings,
		settingSchema: opts.settingSchema,
		saveSettings,
	};
}

// ----- Pure helpers (kept here so they can be unit-tested) -----

/** Reduce a single AgentEvent into a new transcript. */
export function reduceEvent(current: ChatMessage[], event: AgentEvent, assistantId: string): ChatMessage[] {
	const next = current.slice();
	const index = next.findIndex((m) => m.id === assistantId);

	switch (event.type) {
		case "message_start": {
			if (event.message.role === "assistant" && index < 0) {
				next.push({
					id: assistantId,
					role: "assistant",
					text: deriveText(event.message),
					status: "streaming",
					tools: [],
					timestamp: event.message.timestamp,
				});
			} else if (event.message.role === "assistant" && index >= 0) {
				// Re-start of the same assistant message (e.g. server resume or
				// a re-emit): reset its tool list so a duplicate tool card
				// doesn't sneak in alongside the previous run.
				const target = next[index];
				target.text = deriveText(event.message);
				target.status = "streaming";
				target.tools = [];
			}
			break;
		}
		case "message_update": {
			const inner = event.assistantMessageEvent;
			let target = next.find((m) => m.id === assistantId);
			if (!target) {
				target = { id: assistantId, role: "assistant", text: "", status: "streaming", tools: [] };
				next.push(target);
			}
			if (inner.type === "text_delta") {
				target.text = (target.text ?? "") + inner.delta;
			} else if (inner.type === "text_end") {
				target.text = inner.content;
			}
			break;
		}
		case "message_end": {
			const target = next.find((m) => m.id === assistantId);
			if (target && event.message.role === "assistant") {
				target.text = deriveText(event.message);
				target.status = "done";
			}
			break;
		}
		case "tool_execution_start": {
			let target = next.find((m) => m.id === assistantId);
			if (!target) {
				target = { id: assistantId, role: "assistant", text: "", status: "streaming", tools: [] };
				next.push(target);
			}
			// Dedupe by toolCallId — a duplicate start event (replay, race)
			// would otherwise produce two cards with the same React key.
			const existingIndex = target.tools.findIndex((t) => t.id === event.toolCallId);
			const newTool: ToolRun = {
				id: event.toolCallId,
				name: event.toolName,
				args: event.args,
				output: existingIndex >= 0 ? target.tools[existingIndex].output : "",
				status: "running",
				category: toolCategory(event.toolName),
			};
			if (existingIndex >= 0) {
				const tools = target.tools.slice();
				tools[existingIndex] = newTool;
				target.tools = tools;
			} else {
				target.tools = [...target.tools, newTool];
			}
			break;
		}
		case "tool_execution_progress":
		case "tool_execution_end": {
			const target = next.find((m) => m.id === assistantId);
			const tool = target?.tools.find((t) => t.id === event.toolCallId);
			if (tool) {
				const result = event.type === "tool_execution_progress" ? event.progress : event.result;
				tool.output = renderResult(result);
				if (event.type === "tool_execution_end") {
					tool.status = event.isError ? "error" : "done";
				}
			}
			break;
		}
	}

	return next;
}

function renderResult(result: { content?: Array<{ type: string; text?: string }>; details?: unknown } | undefined): string {
	if (!result) return "";
	const text = (result.content ?? [])
		.filter((p) => p?.type === "text" && typeof p.text === "string")
		.map((p) => p.text)
		.join("\n");
	return text || (result.details ? JSON.stringify(result.details, null, 2) : "");
}

function newId(): string {
	if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
	return `id-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}
