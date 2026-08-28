import { describe, it, expect, vi } from "vitest";
import { EventStream, AssistantMessageEventStream, createAssistantMessageEventStream } from "../src/event-stream.ts";
import type { AssistantMessageEvent } from "../src/types.ts";

describe("EventStream", () => {
	it("should push events to consumers in order", async () => {
		const stream = new EventStream<string, string>(
			(e) => e === "done",
			(e) => e,
		);

		const received: string[] = [];
		stream.push("a");
		stream.push("b");
		stream.push("c");
		stream.push("done");

		for await (const event of stream) {
			received.push(event);
		}

		expect(received).toEqual(["a", "b", "c", "done"]);
	});

	it("should resolve result() when isComplete event is pushed", async () => {
		const stream = new EventStream<string, string>(
			(e) => e === "done",
			(e) => e,
		);

		stream.push("done");
		const result = await stream.result();
		expect(result).toBe("done");
	});

	it("should flush waiting consumers immediately on push", async () => {
		const stream = new EventStream<string, string>(
			(e) => e === "done",
			(e) => e,
		);

		const received: string[] = [];
		const consumer = (async () => {
			for await (const e of stream) {
				received.push(e);
			}
		})();

		// Give consumer a microtask to start waiting
		stream.push("first");
		stream.push("done");

		await consumer;
		expect(received).toEqual(["first", "done"]);
	});

	it("should handle end() with explicit result", async () => {
		const stream = new EventStream<string, string>(
			() => false, // never auto-complete
			() => "fallback",
		);

		stream.push("a");
		stream.end("final");

		const received: string[] = [];
		for await (const e of stream) {
			received.push(e);
		}

		const result = await stream.result();
		expect(received).toEqual(["a"]);
		expect(result).toBe("final");
	});

	it("should not push after done", () => {
		const stream = new EventStream<string, string>(
			(e) => e === "done",
			(e) => e,
		);

		stream.push("done");
		// Should be a no-op
		const spy = vi.fn();
		// Verify done flag is set
		stream.push("too late");
		expect(stream.done).toBe(true);
	});
});

describe("AssistantMessageEventStream", () => {
	it("should extract final message from 'done' event", async () => {
		const stream = createAssistantMessageEventStream();

		const finalMessage = {
			role: "assistant" as const,
			content: [{ type: "text" as const, text: "Hello" }],
			api: "faux" as const,
			provider: "faux" as const,
			model: "faux-1",
			usage: { input: 1, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 6, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
			stopReason: "stop" as const,
			timestamp: Date.now(),
		};

		stream.push({ type: "start", partial: { ...finalMessage, content: [] } });
		stream.push({ type: "text_delta", contentIndex: 0, delta: "Hello", partial: finalMessage });
		stream.push({ type: "done", reason: "stop", message: finalMessage });

		const result = await stream.result();
		expect(result.content).toEqual([{ type: "text", text: "Hello" }]);
		expect(result.stopReason).toBe("stop");
	});

	it("should extract error message from 'error' event", async () => {
		const stream = createAssistantMessageEventStream();

		const errMessage = {
			role: "assistant" as const,
			content: [{ type: "text" as const, text: "" }],
			api: "faux" as const,
			provider: "faux" as const,
			model: "faux-1",
			usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
			stopReason: "error" as const,
			errorMessage: "Something went wrong",
			timestamp: Date.now(),
		};

		stream.push({ type: "error", reason: "error", error: errMessage });

		const result = await stream.result();
		expect(result.errorMessage).toBe("Something went wrong");
	});

	it("should yield events in order via async iterator", async () => {
		const stream = createAssistantMessageEventStream();
		const events: AssistantMessageEvent[] = [];

		const msg = { role: "assistant" as const, content: [], api: "faux" as const, provider: "faux" as const, model: "m", usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } }, stopReason: "stop" as const, timestamp: 0 };

		stream.push({ type: "start", partial: msg });
		stream.push({ type: "text_delta", contentIndex: 0, delta: "Hi", partial: { ...msg } });

		const consumer = (async () => {
			for await (const e of stream) {
				events.push(e);
			}
		})();

		stream.push({ type: "done", reason: "stop", message: { ...msg, content: [{ type: "text", text: "Hi" }] } });

		await consumer;
		expect(events[0].type).toBe("start");
		expect(events[1].type).toBe("text_delta");
		expect(events[2].type).toBe("done");
	});
});
