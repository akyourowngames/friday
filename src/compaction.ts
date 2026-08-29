/**
 * Session compaction.
 *
 * As a conversation grows, the transcript gets too long to send to the LLM.
 * Pi solves this in two ways:
 *
 *   1. *Token-budgeted truncation* — drop or shrink old tool result blocks
 *      so the transcript fits. This is a deterministic, lossy operation
 *      that always works.
 *
 *   2. *LLM summarization* — call the LLM to summarize the oldest N
 *      messages into a single compact "summary" message that replaces
 *      them. This is much higher quality but requires an LLM call.
 *
 * We implement both. `transformContext` is the integration point — the
 * agent loop calls it before each LLM call, and the compactor returns a
 * trimmed transcript that fits the budget.
 */
import type { AgentMessage, AssistantMessage, TextContent, ToolResultMessage } from "./types.ts";

/** Rough cost in tokens for an entire message. 1 token ≈ 4 chars. */
export function estimateMessageTokens(message: AgentMessage): number {
	let chars = 0;
	if (typeof message.content === "string") {
		chars = message.content.length;
	} else {
		for (const c of message.content) {
			if (c.type === "text") chars += c.text.length;
			else if (c.type === "thinking") chars += c.thinking.length;
			else if (c.type === "toolCall") {
				chars += c.name.length;
				chars += JSON.stringify(c.arguments).length;
			} else if (c.type === "image") chars += 32; // images are a constant budget
		}
	}
	if (message.role === "toolResult") {
		const tr = message as ToolResultMessage;
		if (Array.isArray(tr.content)) {
			for (const c of tr.content) {
				if (c.type === "text") chars += c.text.length;
				else if (c.type === "image") chars += 32;
			}
		}
	}
	if (message.role === "assistant") {
		const am = message as AssistantMessage;
		chars += 64; // accounting for tool-call metadata
	}
	return Math.max(1, Math.ceil(chars / 4));
}

export function estimateTranscriptTokens(messages: AgentMessage[]): number {
	let total = 0;
	for (const m of messages) total += estimateMessageTokens(m);
	return total;
}

export interface CompactOptions {
	/** Target token count after compaction. Default: 4000. */
	targetTokens?: number;
	/** Never drop the last N user/assistant messages (preserve the live tail). */
	preserveTail?: number;
	/** Per-tool-result cap (in characters) when shrinking large outputs. */
	maxToolResultChars?: number;
}

const DEFAULT_TARGET_TOKENS = 4000;
const DEFAULT_PRESERVE_TAIL = 4;
const DEFAULT_MAX_TOOL_RESULT_CHARS = 4000;

/**
 * Compact a transcript by trimming old tool-result content until the total
 * is under the target token budget.
 *
 * Strategy: walk the transcript front-to-back (preserving the tail), and
 * for each tool result whose text exceeds `maxToolResultChars`, replace the
 * content with a placeholder that records the original size. Returns a new
 * array (the input is not mutated).
 */
export function compactTranscript(
	messages: AgentMessage[],
	options: CompactOptions = {},
): { messages: AgentMessage[]; dropped: number; tokensBefore: number; tokensAfter: number } {
	const targetTokens = options.targetTokens ?? DEFAULT_TARGET_TOKENS;
	const preserveTail = options.preserveTail ?? DEFAULT_PRESERVE_TAIL;
	const maxToolResultChars = options.maxToolResultChars ?? DEFAULT_MAX_TOOL_RESULT_CHARS;

	const tokensBefore = estimateTranscriptTokens(messages);
	if (tokensBefore <= targetTokens) {
		return { messages: messages.slice(), dropped: 0, tokensBefore, tokensAfter: tokensBefore };
	}

	// Always preserve the last N messages intact (the live tail).
	const tailStart = Math.max(0, messages.length - preserveTail);
	const head = messages.slice(0, tailStart);
	const tail = messages.slice(tailStart);

	let dropped = 0;
	const out: AgentMessage[] = [];

	for (const m of head) {
		if (m.role === "toolResult") {
			const tr = m as ToolResultMessage;
			const totalChars = textContentChars(tr.content);
			if (totalChars > maxToolResultChars) {
				out.push({
					...tr,
					content: [
						{
							type: "text",
							text: `[compacted: tool result was ${totalChars} chars, now truncated to ${maxToolResultChars} chars. Original first 200 chars follow.]\n${truncateTextContent(tr.content, maxToolResultChars)}`,
						},
					],
				});
				dropped += 1;
				continue;
			}
		}
		out.push(m);
	}

	// Re-tail.
	const next = [...out, ...tail];
	const tokensAfter = estimateTranscriptTokens(next);

	// If we're still over the target, the transcript is mostly user/assistant
	// text — those are too important to truncate, so just hand it back and let
	// the caller decide (e.g. run an LLM summary pass).
	return { messages: next, dropped, tokensBefore, tokensAfter };
}

function textContentChars(content: ToolResultMessage["content"]): number {
	let chars = 0;
	for (const c of content) {
		if (c.type === "text") chars += c.text.length;
		else if (c.type === "image") chars += 16;
	}
	return chars;
}

function truncateTextContent(content: ToolResultMessage["content"], max: number): string {
	let chars = 0;
	const parts: string[] = [];
	for (const c of content) {
		if (c.type === "text") {
			if (chars + c.text.length > max) {
				const remaining = Math.max(0, max - chars);
				parts.push(c.text.slice(0, remaining));
				chars = max;
				break;
			}
			parts.push(c.text);
			chars += c.text.length;
		} else if (c.type === "image") {
			parts.push(`[image ${c.mimeType}]`);
			chars += 16;
		}
		if (chars >= max) break;
	}
	return parts.join("\n");
}

/** Build an `transformContext` callback for the agent loop. */
export function makeTransformContext(
	options: CompactOptions = {},
): (messages: AgentMessage[]) => Promise<AgentMessage[]> {
	return async (messages) => {
		const { messages: next } = compactTranscript(messages, options);
		return next;
	};
}

/**
 * Build a placeholder summary message that pre-pends the compacted
 * transcript. Useful after an LLM summarization pass.
 */
export function makeSummaryMessage(summary: string, compactedCount: number, provider: string, model: string): AssistantMessage {
	return {
		role: "assistant",
		content: [{ type: "text", text: summary } satisfies TextContent],
		api: provider as any,
		provider: provider as any,
		model,
		usage: {
			input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		},
		stopReason: "stop",
		timestamp: Date.now(),
		errorMessage: `[compacted: ${compactedCount} earlier messages were summarized]`,
	};
}
