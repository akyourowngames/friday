/**
 * OpenAI-compatible provider.
 *
 * One adapter that handles any provider speaking the OpenAI Chat Completions API:
 *   - OpenAI (real)
 *   - Groq
 *   - OpenRouter
 *   - DeepSeek
 *   - Mistral
 *   - Together AI
 *   - Ollama (with /v1 baseUrl)
 *   - any other OpenAI-compatible endpoint
 *
 * Maps `ChatCompletionChunk` deltas → `AssistantMessageEvent`.
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
} from "../types.ts";
import { AssistantMessageEventStream as EventStreamClass } from "../event-stream.ts";
import { registerModel } from "../model.ts";
import { getProvider } from "./registry.ts";

export interface OpenAICompatConfig {
	model: string;
	apiKey?: string;
	baseUrl?: string;
}

/** Create a StreamFn backed by the `openai` SDK pointed at any OpenAI-compatible endpoint. */
export function createOpenAICompatStreamFn(config: OpenAICompatConfig): StreamFn {
	const client = new OpenAI({
		apiKey: config.apiKey || "no-key-required",
		// Pass undefined when no baseUrl is configured so the SDK uses its own default
		baseURL: config.baseUrl || undefined,
		dangerouslyAllowBrowser: true,
	});

	// Register the model so the agent loop can resolve it
	registerModel({
		id: config.model,
		name: config.model,
		api: "openai" as Api,
		provider: "openai" as Api,
		baseUrl: config.baseUrl ?? "https://api.openai.com/v1",
		reasoning: false,
		contextWindow: 8192,
		maxTokens: 4096,
	});

	return (model: Model<Api>, context, options?: StreamOptions) => {
		return openAICompatToStream(client, config.model, model, context, options ?? {});
	};
}

function openAICompatToStream(
	client: OpenAI,
	modelId: string,
	model: Model<Api>,
	context: { systemPrompt?: string; messages: any[]; tools?: any[] },
	options: StreamOptions,
): AssistantMessageEventStream {
	const stream = new EventStreamClass();
	const abortSignal = options.signal;

	// Translate messages
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

	// Translate tools (if any)
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
		...(options.temperature !== undefined ? { temperature: options.temperature } : {}),
		...(tools && tools.length > 0 ? { tools } : {}),
	};

	void runStream();

	async function runStream(): Promise<void> {
		try {
			const openaiStream = await client.chat.completions.create(params, {
				signal: abortSignal,
			});
			if (!openaiStream || typeof (openaiStream as any)[Symbol.asyncIterator] !== "function") {
				throw new Error("Provider did not return a streaming response");
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

			let lastChunk: OpenAI.Chat.Completions.ChatCompletionChunk | undefined;
			for await (const chunk of openaiStream as AsyncIterable<OpenAI.Chat.Completions.ChatCompletionChunk>) {
				const delta = chunk.choices[0]?.delta;
				lastChunk = chunk;
				if (!delta) continue;

				if (delta.content) {
					for (const contentPart of delta.content) {
						const text = typeof contentPart === "string" ? contentPart : (contentPart as any).text;
						if (text) {
							partialMessage = pushTextDelta(partialMessage, text);
							stream.push({
								type: "text_delta",
								contentIndex: 0,
								delta: text,
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
				stream.push({ type: "error", reason: "error", error: partialMessage });
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
			stream.push({ type: "error", reason: "error", error: errMessage });
			stream.end(errMessage);
		}
	}

	return stream;
}

function pushTextDelta(message: AssistantMessage, text: string): AssistantMessage {
	const lastText = message.content[message.content.length - 1];
	if (lastText && lastText.type === "text") {
		lastText.text += text;
	} else {
		message.content.push({ type: "text", text } as TextContent);
	}
	return message;
}

function pushToolCallDelta(message: AssistantMessage, delta: any): AssistantMessage {
	let existing: ToolCall | undefined;
	for (let i = message.content.length - 1; i >= 0; i--) {
		if (message.content[i].type === "toolCall") {
			existing = message.content[i] as ToolCall;
			break;
		}
	}
	if (existing && delta.id) existing.id = delta.id;
	if (existing && delta.function?.name) existing.name = delta.function.name;
	if (existing && delta.function?.arguments) {
		try {
			const parsed = JSON.parse(delta.function.arguments);
			existing.arguments = { ...existing.arguments, ...parsed };
		} catch {
			existing.arguments = { ...existing.arguments, [delta.function.arguments]: undefined };
		}
	} else if (!existing) {
		let args: any = {};
		if (delta.function?.arguments) {
			try { args = JSON.parse(delta.function.arguments); }
			catch { args = { raw: delta.function.arguments }; }
		}
		message.content.push({
			type: "toolCall",
			id: delta.id ?? "",
			name: delta.function?.name ?? "",
			arguments: args,
		} as ToolCall);
	}
	return message;
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

/** Fetch available models from any OpenAI-compatible /v1/models endpoint. */
export async function listOpenAICompatModels(config: { apiKey?: string; baseUrl?: string }): Promise<string[]> {
	const client = new OpenAI({
		apiKey: config.apiKey || "no-key-required",
		baseURL: config.baseUrl,
	});
	try {
		const list = await client.models.list();
		const models: string[] = [];
		for await (const m of list as any) {
			if (m?.id) models.push(m.id);
		}
		return models.sort();
	} catch {
		return [];
	}
}
