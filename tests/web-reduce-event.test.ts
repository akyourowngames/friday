import { describe, it, expect } from "vitest";
import { reduceEvent } from "../web/src/lib/use-chat.ts";
import type { AgentEvent, ChatMessage } from "../web/src/lib/use-chat.ts";

const assistantId = "a1";

function makeTranscript(text: string): ChatMessage[] {
	return [
		{ id: "u1", role: "user", text: "hi", status: "done", tools: [], timestamp: 1 },
		{ id: assistantId, role: "assistant", text, status: "streaming", tools: [] },
	];
}

function deltaEvent(delta: string): AgentEvent {
	return {
		type: "message_update",
		assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta, partial: {} as never },
	} as AgentEvent;
}

describe("reduceEvent (web chat reducer)", () => {
	it("appends text deltas normally", () => {
		let messages = makeTranscript("");
		messages = reduceEvent(messages, deltaEvent("Hey "), assistantId);
		messages = reduceEvent(messages, deltaEvent("world"), assistantId);
		expect(messages[1]!.text).toBe("Hey world");
	});

	// React StrictMode double-invokes state updaters in dev. The reducer must
	// be pure: running the same event against the same snapshot twice must
	// yield the same result, not double-appended text ("GGoott iitt").
	it("is idempotent under StrictMode double invocation", () => {
		const base = makeTranscript("Hey ");
		const once = reduceEvent(base, deltaEvent("there"), assistantId);
		// React re-runs the updater with the ORIGINAL state...
		const twice = reduceEvent(base, deltaEvent("there"), assistantId);
		expect(once[1]!.text).toBe("Hey there");
		expect(twice[1]!.text).toBe("Hey there");
		// ...and the original snapshot must be untouched (no in-place mutation).
		expect(base[1]!.text).toBe("Hey ");
	});

	it("handles cumulative deltas without duplication", () => {
		let messages = makeTranscript("");
		messages = reduceEvent(messages, deltaEvent("Hey"), assistantId);
		messages = reduceEvent(messages, deltaEvent("Hey there"), assistantId);
		expect(messages[1]!.text).toBe("Hey there");
	});
});
