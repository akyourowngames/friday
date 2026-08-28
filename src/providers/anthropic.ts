/**
 * Anthropic Claude provider.
 *
 * Lazy-imports @anthropic-ai/sdk and maps its `messages.stream()` events to
 * friday-ng's AssistantMessageEvent protocol. Handles text deltas, thinking
 * blocks, and tool use blocks.
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

export interface AnthropicConfig {
	model: string;
	apiKey: string;
	/** Optional bearer token sent as `Authorization: Bearer <token>` (some
	 *  gateways require it in addition to `x-api-key`). Falls back to the
	 *  ANTHROPIC_AUTH_TOKEN env var via the SDK. */
	authToken?: string;
	baseUrl?: string;
}

/** Anthropic API version. */
const ANTHROPIC_VERSION = "2023-06-01";
const ANTHROPIC_BETA = "messages-2024-07-01";

/** Create a StreamFn backed by Anthropic's `messages.stream()`. */
export function createAnthropicStreamFn(config: AnthropicConfig): StreamFn {
	// Register the model so the agent loop can resolve it
	registerModel({
		id: config.model,
		name: config.model,
		api: "anthropic" as Api,
		provider: "anthropic" as Api,
		baseUrl: config.baseUrl ?? "https://api.anthropic.com",
		reasoning: false,
		contextWindow: 200000,
		maxTokens: 8192,
	});

	return (model: Model<Api>, context, options?: StreamOptions) => {
		return anthropicToStream(config, model, context, options ?? {});
	};
}

async function anthropicToStream(
	config: AnthropicConfig,
	model: Model<Api>,
	context: { systemPrompt?: string; messages: any[]; tools?: any[] },
	options: StreamOptions,
): Promise<AssistantMessageEventStream> {
	const stream = new EventStreamClass();

	let Anthropic: any;
	try {
		Anthropic = (await import("@anthropic-ai/sdk" as any)).default;
	} catch {
		throw new Error(
			"Anthropic SDK not installed. Run: npm install @anthropic-ai-sdk (or set OPENAI_API_KEY to use OpenAI instead).",
		);
	}

	const client = new Anthropic({
		apiKey: config.apiKey,
		...(config.authToken ? { authToken: config.authToken } : {}),
		baseURL: config.baseUrl,
	});

	// Translate messages — Anthropic uses separate "system" param
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
					blocks.push({
						type: "tool_use",
						id: c.id,
						name: c.name,
						input: c.arguments,
					});
				}
			}
			anthropicMessages.push({ role: "assistant", content: blocks });
		} else if (msg.role === "toolResult") {
			anthropicMessages.push({
				role: "user",
				content: [
					{
						type: "tool_result",
						tool_use_id: msg.toolCallId,
						content: Array.isArray(msg.content)
							? msg.content.map((c: any) => (c.type === "text" ? c.text : "")).join("")
							: msg.content,
						is_error: msg.isError,
					},
				],
			});
		}
	}

	// Translate tools
	const tools = context.tools?.map((t) => ({
		name: t.name,
		description: t.description,
		input_schema: (t as any).parameters,
	}));

	const params: any = {
		model: config.model,
		max_tokens: options.maxTokens ?? 4096,
		messages: anthropicMessages,
		...(context.systemPrompt ? { system: context.systemPrompt } : {}),
		...(tools && tools.length > 0 ? { tools } : {}),
	};

	void runStream();

	async function runStream(): Promise<void> {
		try {
			const anthropicStream = client.messages.stream(params, {
				signal: options.signal,
			});

			let partialMessage: AssistantMessage = {
				role: "assistant",
				content: [],
				api: "anthropic" as Api,
				provider: "anthropic" as Api,
				model: config.model,
				usage: emptyUsage(),
				stopReason: "pending",
				timestamp: Date.now(),
			};

			stream.push({ type: "start", partial: partialMessage });

			// Track current block index and accumulated content
			for await (const event of anthropicStream) {
				switch (event.type) {
					case "content_block_start": {
						const block = event.content_block;
						if (block?.type === "text") {
							partialMessage.content = [...partialMessage.content, { type: "text", text: "" } as TextContent];
						} else if (block?.type === "thinking") {
							partialMessage.content = [...partialMessage.content, { type: "thinking", thinking: "" } as ThinkingContent];
						} else if (block?.type === "tool_use") {
							partialMessage.content = [
								...partialMessage.content,
								{ type: "toolCall", id: block.id, name: block.name, arguments: {} } as ToolCall,
							];
						}
						stream.push({ type: "start", partial: { ...partialMessage } });
						break;
					}
					case "content_block_delta": {
						const delta = event.delta;
						const index = event.index;
						if (delta?.type === "text_delta") {
							const block = partialMessage.content[index] as TextContent;
							if (block) block.text += delta.text;
							stream.push({
								type: "text_delta",
								contentIndex: index,
								delta: delta.text,
								partial: { ...partialMessage },
							});
						} else if (delta?.type === "thinking_delta") {
							const block = partialMessage.content[index] as ThinkingContent;
							if (block) block.thinking += delta.thinking;
							stream.push({
								type: "thinking_delta",
								contentIndex: index,
								delta: delta.thinking,
								partial: { ...partialMessage },
							});
						} else if (delta?.type === "input_json_delta") {
							// Tool use arguments — accumulate JSON
							const block = partialMessage.content[index] as ToolCall;
							if (block) {
								const accumulated = ((block as any)._partialJson ?? "") + (delta.partial_json ?? "");
								(block as any)._partialJson = accumulated;
								try {
									block.arguments = JSON.parse(accumulated);
								} catch {
									// not yet valid JSON — keep raw
									block.arguments = { _partial: accumulated };
								}
							}
							stream.push({
								type: "toolcall_delta",
								contentIndex: index,
								delta: delta.partial_json ?? "",
								partial: { ...partialMessage },
							});
						}
						break;
					}
					case "content_block_stop": {
						const index = event.index;
						const block = partialMessage.content[index];
						if (block?.type === "text") {
							stream.push({ type: "text_end", contentIndex: index, content: block.text, partial: { ...partialMessage } });
						} else if (block?.type === "thinking") {
							stream.push({ type: "thinking_end", contentIndex: index, content: block.thinking, partial: { ...partialMessage } });
						} else if (block?.type === "toolCall") {
							stream.push({ type: "toolcall_end", contentIndex: index, toolCall: block, partial: { ...partialMessage } });
						}
						break;
					}
					case "message_delta": {
						if (event.usage) {
							partialMessage.usage = {
								input: event.usage.input_tokens ?? partialMessage.usage.input,
								output: event.usage.output_tokens ?? partialMessage.usage.output,
								cacheRead: event.usage.cache_read_input_tokens ?? 0,
								cacheWrite: event.usage.cache_creation_input_tokens ?? 0,
								totalTokens: (event.usage.input_tokens ?? 0) + (event.usage.output_tokens ?? 0),
								cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
							};
						}
						break;
					}
					case "message_stop": {
						const stopReason = event.message?.stop_reason;
						const mapped = mapStopReason(stopReason);
						partialMessage.stopReason = mapped;
						if (mapped === "error") {
							stream.push({ type: "error", reason: "error", error: partialMessage });
						} else {
							stream.push({ type: "done", reason: mapped, message: partialMessage });
						}
						stream.end(partialMessage);
						return;
					}
				}
			}

			// Fallback done
			partialMessage.stopReason = "stop";
			stream.push({ type: "done", reason: "stop", message: partialMessage });
			stream.end(partialMessage);
		} catch (error: unknown) {
			const errMessage: AssistantMessage = {
				role: "assistant",
				content: [],
				api: "anthropic" as Api,
				provider: "anthropic" as Api,
				model: config.model,
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

/** Fetch available Anthropic models (from the models.list endpoint). */
export async function listAnthropicModels(config: { apiKey: string; authToken?: string; baseUrl?: string }): Promise<string[]> {
	const base = (config.baseUrl ?? "https://api.anthropic.com").replace(/\/+$/, "");
	const url = `${base}/v1/models`;
	// Only custom (non-Anthropic) bases may need an admin bearer token injected
	// below; never mutate auth for the real Anthropic API.
	const isCustom = base !== "https://api.anthropic.com";

	const tryFetch = async (bearer?: string): Promise<string[] | null> => {
		const headers: Record<string, string> = {
			"x-api-key": config.apiKey,
			"anthropic-version": "2023-06-01",
		};
		if (bearer) headers["Authorization"] = `Bearer ${bearer}`;
		try {
			const res = await fetch(url, { headers, signal: AbortSignal.timeout(8000) });
			if (!res.ok) return null;
			const json = (await res.json()) as { data?: Array<{ id?: string }> | undefined };
			const ids = (json.data ?? [])
				.map((m) => m?.id)
				.filter((id): id is string => typeof id === "string" && id.length > 0);
			return ids.length ? ids : null;
		} catch {
			return null;
		}
	};

	// 1) Try the configured auth token. Some gateways expose /v1/models only
	//    behind a fixed admin bearer (e.g. the local `freecc` proxy accepts
	//    `Bearer <apiKey>` but rejects the per-user `sk-…` stream token).
	let ids = await tryFetch(config.authToken);
	// 2) For custom bases, retry using the apiKey as the bearer. This matches
	//    the gateway's admin token without hard-coding it (the config's apiKey
	//    already is the gateway token). Skipped for the real Anthropic API.
	if (!ids && isCustom) ids = await tryFetch(config.apiKey);
	if (ids) return ids.sort();

	// 3) Fallback: the SDK's models.list() (works against the real Anthropic API).
	try {
		const Anthropic = (await import("@anthropic-ai/sdk" as any)).default;
		const client = new Anthropic({
			apiKey: config.apiKey,
			...(config.authToken ? { authToken: config.authToken } : {}),
			baseURL: config.baseUrl,
		});
		const result = await client.models.list();
		const list = (result as any)?.data ?? result ?? [];
		const sdkIds: string[] = [];
		for (const m of list) if (m?.id) sdkIds.push(m.id);
		if (sdkIds.length) return sdkIds.sort();
	} catch {
		// ignore
	}

	// 4) Last resort: hard-coded common Claude models.
	return [
		"claude-3-5-sonnet-latest",
		"claude-3-5-haiku-latest",
		"claude-3-opus-latest",
		"claude-3-sonnet-20240229",
		"claude-3-haiku-20240307",
	];
}

// Silences unused-import warning for ANTHROPIC_VERSION / ANTHROPIC_BETA
void ANTHROPIC_VERSION;
void ANTHROPIC_BETA;
