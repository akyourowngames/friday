/**
 * Regression tests for tool-call streaming through the OpenAI-compatible
 * provider — the path that produced the "empty box on every tool run" bug.
 *
 * The bugs being pinned down:
 *  1. `function.arguments` arrives as string fragments (`{"comm` + `and":…`).
 *     Parsing per-fragment corrupted the arguments so every tool call failed
 *     validation. They must be concatenated and parsed once.
 *  2. Assistant messages were sent back to the API WITHOUT `tool_calls`,
 *     orphaning the tool results that followed (a 400 on most gateways).
 *  3. A missing `finish_reason` was mapped to `error`, killing good replies.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Type } from "typebox";
import type { AgentEvent, AgentEventSink, Model, Tool } from "../src/types.ts";

const { createOpenAICompatStreamFn } = await import("../src/providers/openai-compat.ts");
const { runAgentLoop } = await import("../src/agent-loop.ts");

const testModel: Model = {
	id: "test-model",
	name: "Test Model",
	api: "openai",
	provider: "openai",
	baseUrl: "http://localhost:9999/v1",
	reasoning: false,
	input: ["text"],
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	contextWindow: 8192,
	maxTokens: 4096,
};

/** Build one SSE data line from a choices delta/finish payload. */
function sse(choice: Record<string, unknown>): string {
	return `data: ${JSON.stringify({
		id: "x",
		object: "chat.completion.chunk",
		model: "test-model",
		choices: [{ index: 0, ...choice }],
	})}\n\n`;
}

function sseResponse(bodies: string[]): Response {
	const body = bodies.join("") + "data: [DONE]\n\n";
	const stream = new ReadableStream<Uint8Array>({
		start(controller) {
			controller.enqueue(new TextEncoder().encode(body));
			controller.close();
		},
	});
	return new Response(stream, {
		status: 200,
		headers: { "content-type": "text/event-stream" },
	});
}

/** A fetch stub whose Nth call returns the Nth prepared SSE response.
 *  Captures every request body so tests can assert the wire format. */
function scriptedFetch(responses: string[][]): {
	fetch: typeof globalThis.fetch;
	requests: { url: string; body: any }[];
} {
	const requests: { url: string; body: any }[] = [];
	let call = 0;
	const fetchMock = vi.fn(async (input: any, init?: any) => {
		try {
			requests.push({
				url: String(input),
				body: init?.body ? JSON.parse(init.body) : null,
			});
		} catch {
			requests.push({ url: String(input), body: null });
		}
		const next = responses[Math.min(call, responses.length - 1)]!;
		call++;
		return sseResponse(next);
	}) as unknown as typeof globalThis.fetch;
	return { fetch: fetchMock, requests };
}

const echoTool: Tool = {
	name: "echo",
	description: "Echo the given text back.",
	parameters: Type.Object({ text: Type.String() }),
	isReadOnly: true,
	async execute(_id, params) {
		return { content: [{ type: "text" as const, text: `echo: ${params.text}` }] };
	},
};

describe("openai-compat tool-call streaming", () => {
	let originalFetch: typeof globalThis.fetch;
	beforeEach(() => {
		originalFetch = globalThis.fetch;
	});
	afterEach(() => {
		globalThis.fetch = originalFetch;
		vi.restoreAllMocks();
	});

	it("assembles fragmented tool-call arguments into valid JSON (the empty-box bug)", async () => {
		globalThis.fetch = scriptedFetch([
			[
				sse({ delta: { role: "assistant", tool_calls: [{ index: 0, id: "call_1", type: "function", function: { name: "echo", arguments: "" } }] }, finish_reason: null }),
				sse({ delta: { tool_calls: [{ index: 0, function: { arguments: '{"te' } }] }, finish_reason: null }),
				sse({ delta: { tool_calls: [{ index: 0, function: { arguments: 'xt":"hello"}' } }] }, finish_reason: "tool_calls" }),
			],
			[
				sse({ delta: { content: "ok" }, finish_reason: null }),
				sse({ delta: {}, finish_reason: "stop" }),
			],
		]).fetch;

		const streamFn = createOpenAICompatStreamFn({ model: "test-model", apiKey: "sk-test", baseUrl: "http://localhost:9999/v1" });
		const events: AgentEvent[] = [];
		const emit: AgentEventSink = async (e) => events.push(e);
		const config = {
			model: testModel,
			convertToLlm: (m: any[]) => m,
			streamFunction: streamFn,
			toolExecution: "sequential" as const,
		} as any;

		const result = await runAgentLoop(
			[{ role: "user", content: "say hello via the tool", timestamp: Date.now() }],
			{ systemPrompt: "test", messages: [], tools: [echoTool] },
			config,
			emit,
			undefined,
			streamFn,
		);

		const assistant = result.find((m) => m.role === "assistant");
		expect(assistant).toBeDefined();
		if (!assistant || assistant.role !== "assistant") throw new Error("expected assistant message");
		const toolCall = assistant.content.find((c) => c.type === "toolCall");
		expect(toolCall).toBeDefined();
		if (!toolCall || toolCall.type !== "toolCall") throw new Error("expected toolCall content");
		// THE regression: arguments must be the parsed object, not fragments
		// keyed as `{"te": undefined, "xt":"hello"}: undefined`.
		expect(toolCall.arguments).toEqual({ text: "hello" });
		expect(toolCall.name).toBe("echo");
		expect(toolCall.id).toBe("call_1");
		expect(assistant.stopReason).toBe("toolUse");

		// The agent loop executed the tool with the correct arguments.
		const toolEnd = events.find((e) => e.type === "tool_execution_end");
		expect(toolEnd).toBeDefined();
		if (toolEnd && toolEnd.type === "tool_execution_end") {
			expect(toolEnd.isError).toBe(false);
			const text = (toolEnd.result.content[0] as any).text;
			expect(text).toBe("echo: hello");
		}
	});

	it("infers toolUse when the gateway omits finish_reason", async () => {
		globalThis.fetch = scriptedFetch([
			[
				sse({ delta: { tool_calls: [{ index: 0, id: "c1", type: "function", function: { name: "echo", arguments: '{"text":"hi"}' } }] }, finish_reason: null }),
				sse({ delta: {}, finish_reason: null }),
			],
			[
				sse({ delta: { content: "ok" }, finish_reason: null }),
				sse({ delta: {}, finish_reason: "stop" }),
			],
		]).fetch;

		const streamFn = createOpenAICompatStreamFn({ model: "test-model", apiKey: "sk-test", baseUrl: "http://localhost:9999/v1" });
		const events: AgentEvent[] = [];
		const emit: AgentEventSink = async (e) => events.push(e);
		const config = { model: testModel, convertToLlm: (m: any[]) => m, streamFunction: streamFn, toolExecution: "sequential" as const } as any;

		const result = await runAgentLoop(
			[{ role: "user", content: "go", timestamp: Date.now() }],
			{ systemPrompt: "test", messages: [], tools: [echoTool] },
			config,
			emit,
			undefined,
			streamFn,
		);
		const assistant = result.find((m) => m.role === "assistant");
		if (!assistant || assistant.role !== "assistant") throw new Error("expected assistant");
		expect(assistant.stopReason).toBe("toolUse");
	});

	it("echoes assistant tool_calls back to the API alongside tool results (round-trip)", async () => {
		const { fetch, requests } = scriptedFetch([
			[
				sse({ delta: { tool_calls: [{ index: 0, id: "call_9", type: "function", function: { name: "echo", arguments: '{"text":"once"}' } }] }, finish_reason: "tool_calls" }),
			],
			[
				sse({ delta: { content: "all done" }, finish_reason: null }),
				sse({ delta: {}, finish_reason: "stop" }),
			],
		]);
		globalThis.fetch = fetch;

		const streamFn = createOpenAICompatStreamFn({ model: "test-model", apiKey: "sk-test", baseUrl: "http://localhost:9999/v1" });
		const events: AgentEvent[] = [];
		const emit: AgentEventSink = async (e) => events.push(e);
		const config = { model: testModel, convertToLlm: (m: any[]) => m, streamFunction: streamFn, toolExecution: "sequential" as const } as any;

		const result = await runAgentLoop(
			[{ role: "user", content: "use the tool", timestamp: Date.now() }],
			{ systemPrompt: "test", messages: [], tools: [echoTool] },
			config,
			emit,
			undefined,
			streamFn,
		);

		// Two API calls happened: the tool-call turn and the follow-up turn.
		expect(requests.length).toBe(2);

		// THE round-trip regression: the second request must contain an
		// assistant message WITH tool_calls, followed by a tool result that
		// references the same tool_call_id.
		const second = requests[1]!.body;
		const wire = second.messages as any[];
		const assistantMsg = wire.find((m: any) => m.role === "assistant" && m.tool_calls);
		expect(assistantMsg).toBeDefined();
		expect(assistantMsg.tool_calls[0].id).toBe("call_9");
		expect(assistantMsg.tool_calls[0].function.name).toBe("echo");
		expect(JSON.parse(assistantMsg.tool_calls[0].function.arguments)).toEqual({ text: "once" });

		const toolMsg = wire.find((m: any) => m.role === "tool");
		expect(toolMsg).toBeDefined();
		expect(toolMsg.tool_call_id).toBe("call_9");
		expect(toolMsg.content).toBe("echo: once");

		// And the loop finished with the model's final text.
		const last = result[result.length - 1];
		if (last.role !== "assistant") throw new Error("expected final assistant message");
		const text = last.content.filter((c: any) => c.type === "text").map((c: any) => c.text).join("");
		expect(text).toBe("all done");
	});
});
