/**
 * Google Gemini provider.
 *
 * Lazy-imports @google/genai and maps its `generateContentStream()` events to
 * friday-ng's AssistantMessageEvent protocol. Supports text, thinking (thoughts),
 * and function calling.
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

export interface GoogleConfig {
	model: string;
	apiKey: string;
	baseUrl?: string;
}

/** Create a StreamFn backed by Google's `generateContentStream()`. */
export function createGoogleStreamFn(config: GoogleConfig): StreamFn {
	registerModel({
		id: config.model,
		name: config.model,
		api: "google" as Api,
		provider: "google" as Api,
		baseUrl: config.baseUrl ?? "https://generativelanguage.googleapis.com/v1beta",
		reasoning: false,
		contextWindow: 1000000,
		maxTokens: 8192,
	});

	return (model: Model<Api>, context, options?: StreamOptions) => {
		return googleToStream(config, model, context, options ?? {});
	};
}

async function googleToStream(
	config: GoogleConfig,
	model: Model<Api>,
	context: { systemPrompt?: string; messages: any[]; tools?: any[] },
	options: StreamOptions,
): Promise<AssistantMessageEventStream> {
	const stream = new EventStreamClass();

	let GoogleGenAI: any;
	try {
		const mod = await import("@google/genai" as any);
		GoogleGenAI = mod.GoogleGenAI;
	} catch {
		throw new Error(
			"Google GenAI SDK not installed. Run: npm install @google/genai (or set OPENAI_API_KEY to use OpenAI instead).",
		);
	}

	const ai = new GoogleGenAI({ apiKey: config.apiKey });

	// Translate messages to Gemini's Content[] format
	const contents: any[] = [];
	if (context.messages) {
		for (const msg of context.messages) {
			if (msg.role === "user") {
				contents.push({
					role: "user",
					parts: Array.isArray(msg.content)
						? msg.content.map((c: any) => (c.type === "text" ? { text: c.text } : { inlineData: { data: c.data, mimeType: c.mimeType } }))
						: [{ text: msg.content }],
				});
			} else if (msg.role === "assistant") {
				const parts: any[] = [];
				for (const c of msg.content) {
					if (c.type === "text") parts.push({ text: c.text });
					else if (c.type === "thinking") parts.push({ thought: c.thinking });
					else if (c.type === "toolCall") parts.push({ functionCall: { name: c.name, args: c.arguments, id: c.id } });
				}
				contents.push({ role: "model", parts });
			} else if (msg.role === "toolResult") {
				contents.push({
					role: "function",
					parts: [
						{
							functionResponse: {
								name: msg.toolName,
								response: {
									result: Array.isArray(msg.content)
										? msg.content.map((c: any) => (c.type === "text" ? c.text : "")).join("")
										: msg.content,
								},
							},
						},
					],
				});
			}
		}
	}

	// Translate tools
	const tools = context.tools?.map((t) => ({
		functionDeclarations: [
			{
				name: t.name,
				description: t.description,
				parametersJsonSchema: (t as any).parameters,
			},
		],
	}));

	const config_ = {
		...(context.systemPrompt ? { systemInstruction: { parts: [{ text: context.systemPrompt }] } } : {}),
		...(tools && tools.length > 0 ? { tools } : {}),
		...(options.maxTokens ? { maxOutputTokens: options.maxTokens } : {}),
		...(options.temperature !== undefined ? { temperature: options.temperature } : {}),
	};

	void runStream();

	async function runStream(): Promise<void> {
		try {
			const result = await ai.models.generateContentStream({
				model: config.model,
				contents,
				config: config_,
			});

			let partialMessage: AssistantMessage = {
				role: "assistant",
				content: [],
				api: "google" as Api,
				provider: "google" as Api,
				model: config.model,
				usage: emptyUsage(),
				stopReason: "pending",
				timestamp: Date.now(),
			};

			stream.push({ type: "start", partial: partialMessage });

			for await (const chunk of result) {
				const candidates = (chunk as any).candidates;
				if (!candidates || candidates.length === 0) continue;
				const cand = candidates[0];
				const parts = cand?.content?.parts ?? [];

				for (let i = 0; i < parts.length; i++) {
					const part = parts[i];
					if (part?.text !== undefined) {
						// Could be regular text OR a thought
						const isThought = part.thought === true;
						if (isThought) {
							const block: ThinkingContent = { type: "thinking", thinking: part.text };
							partialMessage.content = [...partialMessage.content, block];
							stream.push({
								type: "thinking_delta",
								contentIndex: partialMessage.content.length - 1,
								delta: part.text,
								partial: { ...partialMessage },
							});
						} else {
							const block: TextContent = { type: "text", text: part.text };
							partialMessage.content = [...partialMessage.content, block];
							stream.push({
								type: "text_delta",
								contentIndex: partialMessage.content.length - 1,
								delta: part.text,
								partial: { ...partialMessage },
							});
						}
					} else if (part?.functionCall) {
						const tc: ToolCall = {
							type: "toolCall",
							id: part.functionCall.id ?? `call_${Date.now()}`,
							name: part.functionCall.name,
							arguments: part.functionCall.args ?? {},
						};
						partialMessage.content = [...partialMessage.content, tc];
						stream.push({
							type: "toolcall_end",
							contentIndex: partialMessage.content.length - 1,
							toolCall: tc,
							partial: { ...partialMessage },
						});
					}
				}

				// Update usage if present
				const usage = (chunk as any).usageMetadata;
				if (usage) {
					partialMessage.usage = {
						input: usage.promptTokenCount ?? 0,
						output: usage.candidatesTokenCount ?? 0,
						cacheRead: usage.cachedContentTokenCount ?? 0,
						cacheWrite: 0,
						totalTokens: usage.totalTokenCount ?? 0,
						cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
					};
				}
			}

			// Determine stop reason from finishReason
			const lastFinishReason = (result as any).response?.candidates?.[0]?.finishReason;
			partialMessage.stopReason = mapFinishReason(lastFinishReason);
			const hasToolCalls = partialMessage.content.some((c) => c.type === "toolCall");
			if (hasToolCalls && partialMessage.stopReason === "stop") {
				partialMessage.stopReason = "toolUse";
			}
			if (partialMessage.stopReason === "error") {
				stream.push({ type: "error", reason: "error", error: partialMessage });
			} else {
				stream.push({
					type: "done",
					reason: partialMessage.stopReason as "stop" | "length" | "toolUse" | "deferred",
					message: partialMessage,
				});
			}
			stream.end(partialMessage);
		} catch (error: unknown) {
			const errMessage: AssistantMessage = {
				role: "assistant",
				content: [],
				api: "google" as Api,
				provider: "google" as Api,
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

function mapFinishReason(reason: string | undefined): "stop" | "length" | "toolUse" | "error" {
	switch (reason) {
		case "STOP":
		case "FINISH_REASON_UNSPECIFIED":
		case "MAX_TOKENS":
			return "stop";
		case "SAFETY":
		case "RECITATION":
		case "BLOCKLIST":
		case "PROHIBITED_CONTENT":
		case "SPII":
			return "length";
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

/** Fetch available Gemini models. Falls back to a hard-coded list if API is unreachable. */
export async function listGoogleModels(config: { apiKey: string; baseUrl?: string }): Promise<string[]> {
	try {
		const mod = await import("@google/genai" as any);
		const ai = new mod.GoogleGenAI({ apiKey: config.apiKey });
		const pager = await ai.models.list();
		const ids: string[] = [];
		for await (const m of pager as any) {
			if (m?.name) ids.push(m.name.replace(/^models\//, ""));
		}
		return ids.sort();
	} catch {
		return [
			"gemini-2.0-flash",
			"gemini-2.0-flash-lite",
			"gemini-1.5-pro",
			"gemini-1.5-flash",
			"gemini-1.5-flash-8b",
		];
	}
}
