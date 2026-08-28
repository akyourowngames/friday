/**
 * OpenAI provider adapter for friday-ng.
 *
 * Wraps the official `openai` npm package and converts its streaming
 * SSE events into the assistant-message event-stream protocol used by
 * the Pi agent harness.
 */
import OpenAI from "openai";
import type {
	Api,
	AssistantMessage,
	AssistantMessageEventStream,
	Model,
	StreamFn,
	StreamOptions,
	TextContent,
	ToolCall,
	Usage,
} from "./types.ts";
import { AssistantMessageEventStream as EventStreamClass } from "./event-stream.ts";

export interface OpenAIStreamConfig {
	model: string;
	apiKey: string;
	baseUrl?: string;
}

/**
 * Create a `StreamFn` backed by the official OpenAI SDK.
 *
 * Each delta chunk from the OpenAI stream is pushed as a
 * `text_delta` / `thinking_delta` / `toolcall_delta` event, so
 * tokens appear instantly — no buffering.
 */
export function createOpenAIStreamFn(config: OpenAIStreamConfig): StreamFn {
	const client = new OpenAI({
		apiKey: config.apiKey,
		baseURL: config.baseUrl,
	});

	return (model: Model<Api>, context, options?: StreamOptions) => {
		return createOpenAIStream(client, config.model, model, context, options ?? {});
	};
}

function createOpenAIStream(
	client: OpenAI,
	modelId: string,
	model: Model<Api>,
	context: { systemPrompt?: string; messages: any[]; tools?: any[] },
	options: StreamOptions,
): AssistantMessageEventStream {
	const stream = new EventStreamClass();
	const abortSignal = options.signal;

	// Build the messages array for the OpenAI API.
	const messages: OpenAI.Chat.Completions.ChatCompletionMessageParam[] = [];
	if (context.systemPrompt) {
		messages.push({ role: "system", content: context.systemPrompt });
	}
	for (const msg of context.messages) {
		if (msg.role === "user") {
			messages.push({ role: "user", content: typeof msg.content === "string" ? msg.content : msg.content });
		} else if (msg.role === "assistant") {
			const contentParts: OpenAI.Chat.Completions.ChatCompletionContentPart[] = [];
			for (const c of msg.content) {
				if (c.type === "text") {
					contentParts.push({ type: "text" as const, text: c.text } as any);
				}
			}
			messages.push({ role: "assistant", content: contentParts } as any);
		} else if (msg.role === "toolResult") {
			messages.push({
				role: "tool",
				tool_call_id: msg.toolCallId,
				content: Array.isArray(msg.content)
					? msg.content.map((c: any) => (c.type === "text" ? c.text : "")).join("")
					: msg.content,
			} as any);
		}
	}

	const tools = context.tools?.map((t) => ({
		type: "function" as const,
		function: {
			name: t.name,
			description: t.description,
			parameters: (t as any).parameters,
		},
	}));

	const params: OpenAI.Chat.Completions.ChatCompletionCreateParams = {
		model: modelId,
		messages,
		stream: true,
		...(options.maxTokens ? { max_tokens: options.maxTokens } : {}),
		...(options.temperature ? { temperature: options.temperature } : {}),
		...(tools ? { tools } : {}),
	};

	// Spawn the async consumer — it pushes events into the stream as OpenAI
	// yields chunks, ensuring tokens appear instantly.
	void openaiToStream();

	async function openaiToStream(): Promise<void> {
		try {
			let lastChunk: OpenAI.Chat.Completions.ChatCompletionChunk | undefined;
			const openaiStream = await client.chat.completions.create(params, {
				signal: abortSignal,
			});
			if (!openaiStream || typeof (openaiStream as any)[Symbol.asyncIterator] !== "function") {
				throw new Error("OpenAI did not return a streaming response");
			}

			let partialMessage: AssistantMessage = {
				role: "assistant",
				content: [],
				api: "openai" as Api,
				provider: "openai" as Api,
				model: modelId,
				usage: emptyUsage(),
				stopReason: "pending",
				timestamp: Date.now(),
			};

			stream.push({ type: "start", partial: partialMessage });

			for await (const chunk of openaiStream as AsyncIterable<OpenAI.Chat.Completions.ChatCompletionChunk>) {
				const delta = chunk.choices[0]?.delta;
				lastChunk = chunk;
				if (!delta) continue;

				if (delta.content) {
					for (const contentPart of delta.content) {
						if (typeof contentPart === "string") {
							partialMessage = pushTextDelta(partialMessage, contentPart);
							stream.push({
								type: "text_delta",
								contentIndex: 0,
								delta: contentPart,
								partial: partialMessage,
							});
						} else if ((contentPart as any).type === "text" && (contentPart as any).text) {
							partialMessage = pushTextDelta(partialMessage, (contentPart as any).text);
							stream.push({
								type: "text_delta",
								contentIndex: 0,
								delta: (contentPart as any).text,
								partial: partialMessage,
							});
						}
					}
				}

				if (delta.tool_calls) {
					for (const tc of delta.tool_calls) {
						if (tc.type === "function" && tc.function?.arguments) {
							partialMessage = pushToolCallDelta(partialMessage, tc as any);
							stream.push({
								type: "toolcall_delta",
								contentIndex: 0,
								delta: tc.function.arguments,
								partial: partialMessage,
							});
						}
					}
				}

				if (chunk.usage) {
					partialMessage.usage = mapUsage(chunk.usage);
				}
			}

			const finishReason = lastChunk?.choices[0]?.finish_reason ?? null;
			const stopReason = mapFinishReason(finishReason);
			partialMessage.stopReason = stopReason === "error" ? "error" : (stopReason as "stop" | "length" | "toolUse");
			if (stopReason === "error") {
				stream.push({
					type: "error",
					reason: "error",
					error: partialMessage,
				});
			} else {
				stream.push({
					type: "done",
					reason: stopReason as "stop" | "length" | "toolUse" | "deferred",
					message: partialMessage,
				});
			}
			stream.end(partialMessage);
		} catch (error: unknown) {
			const errMessage: AssistantMessage = {
				role: "assistant",
				content: [],
				api: "openai" as Api,
				provider: "openai" as Api,
				model: modelId,
				usage: emptyUsage(),
				stopReason: "error",
				errorMessage: error instanceof Error ? error.message : String(error),
				timestamp: Date.now(),
			};
			stream.push({
				type: "error",
				reason: "error",
				error: errMessage,
			});
			stream.end(errMessage);
		}
	}

	return stream;
}

function pushTextDelta(message: AssistantMessage, text: string): AssistantMessage {
	const content = [...message.content];
	const lastText = content[content.length - 1];
	if (lastText && lastText.type === "text") {
		lastText.text += text;
	} else {
		const newText: TextContent = { type: "text", text };
		content.push(newText);
	}
	return { ...message, content };
}

function pushToolCallDelta(message: AssistantMessage, delta: any): AssistantMessage {
	const content = [...message.content];
	let existing: ToolCall | undefined;
	for (let i = content.length - 1; i >= 0; i--) {
		if (content[i].type === "toolCall") {
			existing = content[i] as ToolCall;
			break;
		}
	}
	if (existing && delta.id) {
		existing.id = delta.id;
	}
	if (existing && delta.function?.name) {
		existing.name = delta.function.name;
	}
	if (existing && delta.function?.arguments) {
		try {
			const parsed = JSON.parse(delta.function.arguments);
			existing.arguments = { ...existing.arguments, ...parsed };
		} catch {
			existing.arguments = { ...existing.arguments, [delta.function.arguments]: undefined };
		}
	} else if (!existing) {
		const newToolCall: ToolCall = {
			type: "toolCall",
			id: delta.id ?? "",
			name: delta.function?.name ?? "",
			arguments: delta.function?.arguments ? (() => { try { return JSON.parse(delta.function.arguments); } catch { return { raw: delta.function.arguments }; } })() : {},
		};
		content.push(newToolCall);
	}
	return { ...message, content };
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

function mapUsage(u: OpenAI.CompletionUsage): Usage {
	return {
		input: u.prompt_tokens,
		output: u.completion_tokens,
		cacheRead: u.prompt_tokens_details?.cached_tokens ?? 0,
		cacheWrite: 0,
		totalTokens: u.total_tokens,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	};
}

function mapFinishReason(reason: string | null | undefined): "stop" | "length" | "toolUse" | "deferred" | "error" {
	switch (reason) {
		case "stop":
			return "stop";
		case "length":
			return "length";
		case "tool_calls":
			return "toolUse";
		default:
			return "error";
	}
}
