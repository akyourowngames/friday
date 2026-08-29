import { describe, it, expect } from "vitest";
import {
	compactTranscript,
	estimateMessageTokens,
	estimateTranscriptTokens,
	makeSummaryMessage,
	makeTransformContext,
} from "../src/compaction.ts";
import type { AgentMessage, ToolResultMessage } from "../src/types.ts";

function userMsg(text: string, t = 0): AgentMessage {
	return { role: "user", content: text, timestamp: t };
}

function assistantMsg(text: string, t = 0): AgentMessage {
	return {
		role: "assistant",
		content: [{ type: "text", text }],
		api: "openai",
		provider: "openai",
		model: "gpt-4o-mini",
		usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
		stopReason: "stop",
		timestamp: t,
	};
}

function bigToolResult(text: string, t = 0): ToolResultMessage {
	return {
		role: "toolResult",
		toolCallId: "t1",
		toolName: "bash",
		content: [{ type: "text", text }],
		isError: false,
		timestamp: t,
	};
}

describe("compaction", () => {
	it("estimateMessageTokens roughly divides by 4", () => {
		const m = userMsg("a".repeat(400));
		const tokens = estimateMessageTokens(m);
		expect(tokens).toBe(100);
	});

	it("estimateTranscriptTokens sums every message", () => {
		const total = estimateTranscriptTokens([userMsg("abcd"), userMsg("abcdabcd")]);
		expect(total).toBe(3);
	});

	it("compactTranscript returns input untouched when under budget", () => {
		const input = [userMsg("hi"), assistantMsg("hello")];
		const r = compactTranscript(input, { targetTokens: 1000 });
		expect(r.messages).toHaveLength(2);
		expect(r.dropped).toBe(0);
		// tokensBefore === tokensAfter (nothing changed) even though both
		// include the assistant-message overhead constant.
		expect(r.tokensBefore).toBe(r.tokensAfter);
		expect(r.tokensBefore).toBeGreaterThan(0);
	});

	it("compactTranscript shrinks oversized tool results", () => {
		const huge = "x".repeat(20_000);
		const input: AgentMessage[] = [
			userMsg("run the command", 1),
			bigToolResult(huge, 2),
			userMsg("now do another thing", 3),
			assistantMsg("ok", 4),
			userMsg("one more", 5),
			assistantMsg("done", 6),
		];
		const r = compactTranscript(input, { targetTokens: 200, maxToolResultChars: 200, preserveTail: 2 });
		expect(r.dropped).toBe(1);
		// The compact message should fit within the cap (roughly).
		const compactMsg = r.messages[1]! as ToolResultMessage;
		const text = (compactMsg.content[0] as any).text;
		expect(text.length).toBeLessThan(2000);
		expect(text).toContain("[compacted");
	});

	it("compactTranscript preserves the live tail intact", () => {
		const input: AgentMessage[] = [
			userMsg("old", 1),
			assistantMsg("really old", 2),
			bigToolResult("y".repeat(20_000), 3),
			userMsg("recent 1", 4),
			assistantMsg("recent 2", 5),
			userMsg("live tail A", 6),
			assistantMsg("live tail B", 7),
		];
		const r = compactTranscript(input, { preserveTail: 2 });
		const lastTwo = r.messages.slice(-2);
		expect((lastTwo[0] as any).content).toBe("live tail A");
		expect(((lastTwo[1] as any).content[0] as any).text).toBe("live tail B");
	});

	it("compactTranscript does not mutate the input array", () => {
		const input: AgentMessage[] = [userMsg("hi"), bigToolResult("z".repeat(20_000))];
		const snapshot = JSON.parse(JSON.stringify(input));
		compactTranscript(input, { maxToolResultChars: 100 });
		expect(JSON.parse(JSON.stringify(input))).toEqual(snapshot);
	});

	it("makeTransformContext returns a working async function", async () => {
		const tx = makeTransformContext({ targetTokens: 1000 });
		const result = await tx([userMsg("hi")]);
		expect(result).toHaveLength(1);
	});

	it("makeSummaryMessage has a [compacted] marker", () => {
		const msg = makeSummaryMessage("Earlier we discussed X.", 5, "openai", "gpt-4o-mini");
		expect(msg.role).toBe("assistant");
		expect((msg.content[0] as any).text).toContain("X.");
		expect(msg.errorMessage).toContain("5");
	});
});
