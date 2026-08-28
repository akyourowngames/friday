import { describe, it, expect, vi, beforeEach } from "vitest";
import {
	registerFauxProvider,
	createFauxStreamFn,
	fauxText,
	fauxThinking,
	fauxToolCall,
	fauxAssistantMessage,
	clearFauxProviderState,
} from "../src/provider-faux.ts";
import type { AssistantMessageEvent, Model, Api } from "../src/types.ts";

function makeModel(): Model<Api> {
	return {
		id: "faux-1",
		name: "Faux-1",
		api: "faux",
		provider: "faux",
		baseUrl: "",
		reasoning: false,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		contextWindow: 4096,
		maxTokens: 2048,
	};
}

describe("fauxText", () => {
	it("should create a text content block", () => {
		const block = fauxText("Hello");
		expect(block.type).toBe("text");
		expect(block.text).toBe("Hello");
	});
});

describe("fauxThinking", () => {
	it("should create a thinking content block", () => {
		const block = fauxThinking("Hmm");
		expect(block.type).toBe("thinking");
		expect(block.thinking).toBe("Hmm");
	});
});

describe("fauxToolCall", () => {
	it("should create a toolcall content block", () => {
		const block = fauxToolCall("calculator", { expression: "2 + 2" });
		expect(block.type).toBe("toolCall");
		expect(block.name).toBe("calculator");
		expect(block.arguments).toEqual({ expression: "2 + 2" });
	});
});

describe("fauxAssistantMessage", () => {
	it("should create a complete assistant message", () => {
		const msg = fauxAssistantMessage("Hello world");
		expect(msg.role).toBe("assistant");
		expect(msg.content).toEqual([{ type: "text", text: "Hello world" }]);
		expect(msg.stopReason).toBe("stop");
	});

	it("should set stopReason to toolUse when content has a toolCall", () => {
		const msg = fauxAssistantMessage([fauxToolCall("calc", { x: 1 })]);
		expect(msg.stopReason).toBe("toolUse");
	});
});

describe("registerFauxProvider + createFauxStreamFn", () => {
	beforeEach(() => {
		clearFauxProviderState();
	});

	it("should stream text incrementally via EventStream", async () => {
		const registration = registerFauxProvider({ tokensPerSecond: 1000 });
		registration.setResponses([
			[fauxText("Hello world")],
		]);

		const streamFn = createFauxStreamFn(registration);
		const stream = streamFn(makeModel(), { messages: [], tools: [] });

		const events: AssistantMessageEvent[] = [];
		for await (const event of stream) {
			events.push(event);
		}

		// Should have: start, text_start, text_delta(s), text_end, done
		expect(events.length).toBeGreaterThanOrEqual(3);
		expect(events[0]!.type).toBe("start");
		expect(events.some((e) => e.type === "text_delta")).toBe(true);
		expect(events[events.length - 1]!.type).toBe("done");
	});

	it("should stream thinking events when present", async () => {
		const registration = registerFauxProvider({ tokensPerSecond: 1000 });
		registration.setResponses([
			[fauxThinking("thinking..."), fauxText("Got it!")],
		]);

		const streamFn = createFauxStreamFn(registration);
		const stream = streamFn(makeModel(), { messages: [], tools: [] });

		const events: AssistantMessageEvent[] = [];
		for await (const event of stream) {
			events.push(event);
		}

		expect(events.some((e) => e.type === "thinking_delta")).toBe(true);
		expect(events.some((e) => e.type === "text_delta")).toBe(true);
		expect(events[events.length - 1]!.type).toBe("done");
	});

	it("should stream tool calls", async () => {
		const registration = registerFauxProvider({ tokensPerSecond: 1000 });
		registration.setResponses([
			[fauxToolCall("calculator", { expression: "2 + 2" })],
		]);

		const streamFn = createFauxStreamFn(registration);
		const stream = streamFn(makeModel(), { messages: [], tools: [] });

		const events: AssistantMessageEvent[] = [];
		for await (const event of stream) {
			events.push(event);
		}

		expect(events.some((e) => e.type === "toolcall_start")).toBe(true);
		expect(events.some((e) => e.type === "toolcall_delta")).toBe(true);
		expect(events.some((e) => e.type === "toolcall_end")).toBe(true);
		const done = events.findLast((e) => e.type === "done");
		expect(done).toBeTruthy();
	});

	it("should respect tokensPerSecond rate limiting", async () => {
		const registration = registerFauxProvider({ tokensPerSecond: 100 });
		registration.setResponses([
			[fauxText("a b c d e f g h i j k l m n o p q r s t u v w x y z")],
		]);

		const streamFn = createFauxStreamFn(registration);
		const stream = streamFn(makeModel(), { messages: [], tools: [] });

		const start = Date.now();
		const events: AssistantMessageEvent[] = [];
		for await (const event of stream) {
			events.push(event);
		}
		const elapsed = Date.now() - start;

		// 26 chars = ~6-7 tokens at 100 tps = ~60-70ms minimum
		expect(elapsed).toBeGreaterThan(40);
		expect(events.some((e) => e.type === "text_delta")).toBe(true);
	});

	it("should produce final message with stopReason via result()", async () => {
		const registration = registerFauxProvider({ tokensPerSecond: 1000 });
		registration.setResponses([
			[fauxText("Hello"), fauxText("!")],
		]);

		const streamFn = createFauxStreamFn(registration);
		const stream = streamFn(makeModel(), { messages: [], tools: [] });

		for await (const _ of stream) {
			// drain
		}

		const result = await stream.result();
		// The text may be split across chunks; reconstruct the full text
		const fullText = result.content
			.filter((c) => c.type === "text")
			.map((c) => (c as any).text)
			.join("");
		expect(fullText).toContain("Hello");
		expect(fullText).toContain("!");
		expect(result.stopReason).toBe("stop");
		expect(result.model).toBe("faux-1");
	});

	it("should error when no responses are set", async () => {
		const registration = registerFauxProvider({ tokensPerSecond: 1000 });
		// Don't set responses

		const streamFn = createFauxStreamFn(registration);
		const stream = streamFn(makeModel(), { messages: [], tools: [] });

		const events: AssistantMessageEvent[] = [];
		for await (const event of stream) {
			events.push(event);
		}

		expect(events.some((e) => e.type === "error")).toBe(true);
	});
});
