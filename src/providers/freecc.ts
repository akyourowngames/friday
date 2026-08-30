/**
 * freecc provider — local proxy that speaks the Anthropic Messages protocol
 * but with its own auth scheme.
 *
 * Differences from the real Anthropic API:
 *  - Auth is `Authorization: Bearer <adminToken>` ONLY. `x-api-key` is rejected.
 *  - The `anthropic-version` header is not understood; we must not send it.
 *  - Models are listed from `/v1/models` with the same Bearer token.
 *
 * Because of those constraints we talk to it with hand-rolled `fetch` + SSE
 * rather than the Anthropic SDK (which auto-injects `x-api-key` and
 * `anthropic-version` and can't be cleanly told to skip them).
 */
import type {
	Api,
	AssistantMessage,
	AssistantMessageEventStream,
	Model,
	StreamFn,
	StreamOptions,
	TextContent,
	ThinkingContent,
	ToolCall,
	Usage,
} from "../types.ts";
import { AssistantMessageEventStream as EventStreamClass } from "../event-stream.ts";
import { registerModel } from "../model.ts";

export interface FreeccConfig {
	model: string;
	apiKey: string;
	baseUrl?: string;
}

const ANTHROPIC_BETA = "messages-2024-07-01";

/** Create a StreamFn backed by a fetch + SSE call to the freecc proxy. */
export function createFreeccStreamFn(config: FreeccConfig): StreamFn {
	registerModel({
		id: config.model,
		name: config.model,
		api: "freecc" as Api,
		provider: "freecc" as Api,
		baseUrl: config.baseUrl ?? "http://127.0.0.1:8082",
		reasoning: false,
		contextWindow: 200000,
		maxTokens: 8192,
	});

	return (model: Model<Api>, context, options?: StreamOptions) => {
		return freeccToStream(config, model, context, options ?? {});
	};
}

async function freeccToStream(
	config: FreeccConfig,
	_model: Model<Api>,
	context: { systemPrompt?: string; messages: any[]; tools?: any[] },
	options: StreamOptions,
): Promise<AssistantMessageEventStream> {
	const stream = new EventStreamClass();
	const base = (config.baseUrl ?? "http://127.0.0.1:8082").replace(/\/+$/, "");

	void runStream();

	async function runStream(): Promise<void> {
		const anthropicMessages: any[] = [];
		for (const msg of context.messages) {
			if (msg.role === "user") {
				anthropicMessages.push({
					role: "user",
					content: typeof msg.content === "string" ? msg.content : msg.content,
				});
			} else if (msg.role === "assistant") {
				const blocks: any[] = [];
				for (const c of msg.content) {
					if (c.type === "text") blocks.push({ type: "text", text: c.text });
					else if (c.type === "thinking") blocks.push({ type: "thinking", thinking: c.thinking });
					else if (c.type === "toolCall") {
						blocks.push({ type: "tool_use", id: c.id, name: c.name, input: c.arguments });
					}
				}
				anthropicMessages.push({ role: "assistant", content: blocks });
			} else if (msg.role === "toolResult") {
				const images = msg.content.filter((c: any) => c.type === "image");
				const content = images.length === 0
					? msg.content.map((c: any) => (c.type === "text" ? c.text : "")).join("")
					: msg.content.map((c: any) => c.type === "text"
						? { type: "text", text: c.text }
						: { type: "image", source: { type: "base64", media_type: c.mimeType, data: c.data } });
				anthropicMessages.push({
					role: "user",
					content: [
						{
							type: "tool_result",
							tool_use_id: msg.toolCallId,
							content,
							is_error: msg.isError,
						},
					],
				});
			}
		}

		const tools = context.tools?.map((t) => ({
			name: t.name,
			description: t.description,
			input_schema: (t as any).parameters,
		}));

		const params: any = {
			model: config.model,
			max_tokens: options.maxTokens ?? 4096,
			messages: anthropicMessages,
			stream: true,
			...(context.systemPrompt ? { system: context.systemPrompt } : {}),
			...(tools && tools.length > 0 ? { tools } : {}),
		};

		let partialMessage: AssistantMessage = {
			role: "assistant",
			content: [],
			api: "freecc" as Api,
			provider: "freecc" as Api,
			model: config.model,
			usage: emptyUsage(),
			stopReason: "pending",
			timestamp: Date.now(),
		};

		try {
			const res = await fetch(`${base}/v1/messages`, {
				method: "POST",
				headers: {
					"content-type": "application/json",
					"accept": "text/event-stream",
					"anthropic-beta": ANTHROPIC_BETA,
					"authorization": `Bearer ${config.apiKey}`,
				},
				body: JSON.stringify(params),
				signal: options.signal,
			});

			if (!res.ok || !res.body) {
				const errText = res.body ? await res.text().catch(() => "") : "";
				throw new Error(
					`freecc ${res.status} ${res.statusText}${errText ? `: ${errText.slice(0, 400)}` : ""}`,
				);
			}

			stream.push({ type: "start", partial: partialMessage });

			// The freecc proxy streams Anthropic-format SSE: lines like
			//   event: message_start
			//   data: {"type":"message_start",...}
			const reader = res.body.getReader();
			const decoder = new TextDecoder();
			let buffer = "";
			let eventName = "";

			const processEvent = (name: string, data: string): void => {
				if (!data) return;
				let payload: any;
				try {
					payload = JSON.parse(data);
				} catch {
					return;
				}
				switch (payload.type) {
					case "message_start":
						if (payload.message) {
							partialMessage = {
								...partialMessage,
								...payload.message,
								stopReason: "pending",
							};
						}
						break;
					case "content_block_start": {
						const block = payload.content_block;
						const index = payload.index ?? partialMessage.content.length;
						if (block?.type === "text") {
							partialMessage.content.push({ type: "text", text: "" } as TextContent);
						} else if (block?.type === "thinking") {
							partialMessage.content.push({ type: "thinking", thinking: "" } as ThinkingContent);
						} else if (block?.type === "tool_use") {
							partialMessage.content.push({ type: "toolCall", id: block.id, name: block.name, arguments: {} } as ToolCall);
						}
						stream.push({ type: "text_start", contentIndex: index, partial: partialMessage });
						break;
					}
					case "content_block_delta": {
						const delta = payload.delta;
						const index = payload.index ?? 0;
						if (delta?.type === "text_delta") {
							const block = partialMessage.content[index] as TextContent | undefined;
							if (block && block.type === "text") block.text += delta.text;
							stream.push({
								type: "text_delta",
								contentIndex: index,
								delta: delta.text,
								partial: partialMessage,
							});
						} else if (delta?.type === "thinking_delta") {
							const block = partialMessage.content[index] as ThinkingContent | undefined;
							if (block && block.type === "thinking") block.thinking += delta.thinking;
							stream.push({
								type: "thinking_delta",
								contentIndex: index,
								delta: delta.thinking,
								partial: partialMessage,
							});
						} else if (delta?.type === "input_json_delta") {
							const block = partialMessage.content[index] as ToolCall | undefined;
							if (block && block.type === "toolCall") {
								const accumulated = ((block as any)._partialJson ?? "") + (delta.partial_json ?? "");
								(block as any)._partialJson = accumulated;
								try {
									block.arguments = JSON.parse(accumulated);
								} catch {
									block.arguments = { _partial: accumulated };
								}
							}
							stream.push({
								type: "toolcall_delta",
								contentIndex: index,
								delta: delta.partial_json ?? "",
								partial: partialMessage,
							});
						}
						break;
					}
					case "content_block_stop": {
						const index = payload.index ?? 0;
						const block = partialMessage.content[index];
						if (block?.type === "text") {
							stream.push({
								type: "text_end",
								contentIndex: index,
								content: block.text,
								partial: partialMessage,
							});
						} else if (block?.type === "thinking") {
							stream.push({
								type: "thinking_end",
								contentIndex: index,
								content: block.thinking,
								partial: partialMessage,
							});
						} else if (block?.type === "toolCall") {
							stream.push({
								type: "toolcall_end",
								contentIndex: index,
								toolCall: block,
								partial: partialMessage,
							});
						}
						break;
					}
					case "message_delta":
						if (payload.usage) {
							partialMessage.usage = {
								input: payload.usage.input_tokens ?? partialMessage.usage.input,
								output: payload.usage.output_tokens ?? partialMessage.usage.output,
								cacheRead: payload.usage.cache_read_input_tokens ?? 0,
								cacheWrite: payload.usage.cache_creation_input_tokens ?? 0,
								totalTokens:
									(payload.usage.input_tokens ?? 0) + (payload.usage.output_tokens ?? 0),
								cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
							};
						}
						break;
					case "message_stop": {
						const stopReason = mapStopReason(payload.message?.stop_reason);
						partialMessage.stopReason = stopReason;
						if (stopReason === "error") {
							stream.push({ type: "error", reason: "error", error: partialMessage });
						} else {
							stream.push({ type: "done", reason: stopReason, message: partialMessage });
						}
						stream.end(partialMessage);
						return;
					}
				}
			};

			while (true) {
				const { value, done } = await reader.read();
				if (done) break;
				buffer += decoder.decode(value, { stream: true });
				let lineStart = 0;
				while (lineStart < buffer.length) {
					const newlineIdx = buffer.indexOf("\n", lineStart);
					if (newlineIdx === -1) break;
					const rawLine = buffer.slice(lineStart, newlineIdx);
					lineStart = newlineIdx + 1;
					// strip CR and trim
					const line = rawLine.replace(/\r$/, "");
					if (line.startsWith("event:")) {
						eventName = line.slice(6).trim();
					} else if (line.startsWith("data:")) {
						const data = line.slice(5).trim();
						processEvent(eventName, data);
						eventName = "";
					} else if (line === "") {
						eventName = "";
					}
				}
				buffer = buffer.slice(lineStart);
			}

			// If the stream ended without a message_stop, treat it as a normal
			// completion so the TUI unblocks.
			partialMessage.stopReason = partialMessage.stopReason === "pending" ? "stop" : partialMessage.stopReason;
			stream.push({ type: "done", reason: "stop", message: partialMessage });
			stream.end(partialMessage);
		} catch (error: unknown) {
			if (options.signal?.aborted) {
				partialMessage.stopReason = "aborted";
			} else {
				partialMessage.stopReason = "error";
				partialMessage.errorMessage = error instanceof Error ? error.message : String(error);
			}
			stream.push({ type: "error", reason: "error", error: partialMessage });
			stream.end(partialMessage);
		}
	}

	return stream;
}

function mapStopReason(reason: string | undefined): "stop" | "length" | "toolUse" | "error" {
	switch (reason) {
		case "end_turn":
		case "stop_sequence":
			return "stop";
		case "max_tokens":
		case "model_context_window_exceeded":
			return "length";
		case "tool_use":
			return "toolUse";
		default:
			return "error";
	}
}

function emptyUsage(): Usage {
	return {
		input: 0,
		output: 0,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 0,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	};
}

/** Fetch available models from the freecc proxy. */
export async function listFreeccModels(config: { apiKey: string; baseUrl?: string }): Promise<string[]> {
	const base = (config.baseUrl ?? "http://127.0.0.1:8082").replace(/\/+$/, "");
	const url = `${base}/v1/models`;
	try {
		const res = await fetch(url, {
			headers: { authorization: `Bearer ${config.apiKey}` },
			signal: AbortSignal.timeout(8000),
		});
		if (!res.ok) return [];
		const json = (await res.json()) as { data?: Array<{ id?: string }> };
		const ids = (json.data ?? [])
			.map((m) => m?.id)
			.filter((id): id is string => typeof id === "string" && id.length > 0);
		return ids.sort();
	} catch {
		return [];
	}
}
