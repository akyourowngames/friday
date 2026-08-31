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

	// Translate messages. Assistant tool calls MUST be echoed back with
	// `tool_calls` — otherwise the toolResult messages that follow are
	// orphaned and OpenAI-compatible APIs reject the whole request.
	const messages: OpenAI.Chat.Completions.ChatCompletionMessageParam[] = [];
	if (context.systemPrompt) {
		messages.push({ role: "system", content: context.systemPrompt });
	}
	for (const msg of context.messages) {
		if (msg.role === "user") {
			messages.push({ role: "user", content: typeof msg.content === "string" ? msg.content : msg.content });
		} else if (msg.role === "assistant") {
			const text = (msg.content as any[])
				.filter((c: any) => c.type === "text")
				.map((c: any) => c.text)
				.join("");
			const toolCalls = (msg.content as any[]).filter((c: any) => c.type === "toolCall") as ToolCall[];
			if (toolCalls.length > 0) {
				messages.push({
					role: "assistant",
					content: text || null,
					tool_calls: toolCalls.map((tc) => ({
						id: tc.id,
						type: "function" as const,
						function: { name: tc.name, arguments: JSON.stringify(tc.arguments ?? {}) },
					})),
				} as any);
			} else {
				messages.push({ role: "assistant", content: text } as any);
			}
		} else if (msg.role === "toolResult") {
			const text = msg.content.map((c: any) => (c.type === "text" ? c.text : "")).join("");
			messages.push({
				role: "tool",
				tool_call_id: msg.toolCallId,
				content: text,
			});
			const images = msg.content.filter((c: any) => c.type === "image");
			if (images.length > 0) {
				messages.push({
					role: "user",
					content: [
						{ type: "text", text: `Images returned by tool result ${msg.toolCallId} (${msg.toolName}):` },
						...images.map((image: any) => ({
							type: "image_url" as const,
							image_url: { url: `data:${image.mimeType};base64,${image.data}` },
						})),
					],
				});
			}
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

			// Tool-call arguments arrive as STRING FRAGMENTS across chunks
			// (e.g. `{"comm` + `and":"ls"}`). They must be concatenated and
			// parsed exactly once at the end — parsing per-fragment corrupts
			// the arguments and every tool call fails validation.
			const toolCallAccumulators = new Map<number, { id: string; name: string; args: string }>();

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
							// Guard against endpoints that send the FULL text so far
							// in each chunk instead of just the new fragment. Appending
							// those yields stretched, repeated output ("heeyyyyyyy").
							// Detect a chunk that extends the accumulated text from
							// position 0 and treat it as cumulative: replace, not append.
							const lastTextBlock = partialMessage.content[partialMessage.content.length - 1];
							const accumulated =
								lastTextBlock && lastTextBlock.type === "text" ? lastTextBlock.text : "";
							const isCumulative =
								text.startsWith(accumulated) && text.length > accumulated.length;
							const deltaText = isCumulative ? text.slice(accumulated.length) : text;
							partialMessage = pushTextDelta(partialMessage, deltaText);
							stream.push({
								type: "text_delta",
								contentIndex: 0,
								delta: deltaText,
								partial: partialMessage,
							});
						}
					}
				}

				if (delta.tool_calls) {
					for (const tc of delta.tool_calls) {
						const idx = typeof tc.index === "number" ? tc.index : 0;
						let acc = toolCallAccumulators.get(idx);
						if (!acc) {
							acc = { id: "", name: "", args: "" };
							toolCallAccumulators.set(idx, acc);
						}
						if (tc.id) acc.id = tc.id;
						if (tc.function?.name) acc.name = tc.function.name;
						if (tc.function?.arguments) acc.args += tc.function.arguments;
						stream.push({
							type: "toolcall_delta",
							contentIndex: idx,
							delta: tc.function?.arguments ?? tc.id ?? "",
							partial: partialMessage,
						});
					}
				}

				if (chunk.usage) {
					partialMessage.usage = mapUsage(chunk.usage);
				}
			}

			// Finalize tool calls: parse each accumulated argument string once.
			const finalizedToolCalls: ToolCall[] = [];
			for (const [idx, acc] of [...toolCallAccumulators.entries()].sort((a, b) => a[0] - b[0])) {
				let arguments_: Record<string, any> = {};
				if (acc.args) {
					try {
						arguments_ = JSON.parse(acc.args);
					} catch {
						arguments_ = { raw: acc.args };
					}
				}
				const toolCall: ToolCall = {
					type: "toolCall",
					id: acc.id || `call_${idx}`,
					name: acc.name,
					arguments: arguments_,
				};
				finalizedToolCalls.push(toolCall);
				partialMessage.content.push(toolCall);
				stream.push({
					type: "toolcall_end",
					contentIndex: idx,
					toolCall,
					partial: partialMessage,
				});
			}

			const finishReason = lastChunk?.choices[0]?.finish_reason ?? null;
			const stopReason = mapFinishReason(finishReason, finalizedToolCalls.length > 0);
			partialMessage.stopReason = stopReason;
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

function mapFinishReason(
	reason: string | null | undefined,
	hasToolCalls: boolean,
): "stop" | "length" | "toolUse" | "deferred" | "error" {
	switch (reason) {
		case "stop":
			return "stop";
		case "length":
			return "length";
		case "tool_calls":
		case "function_call":
			return "toolUse";
		case "content_filter":
			return "error";
		default:
			// Many gateways omit finish_reason on the final chunk. Inferring
			// from content (instead of failing) keeps good replies alive —
			// a null finish_reason used to kill every such response.
			return hasToolCalls ? "toolUse" : "stop";
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
