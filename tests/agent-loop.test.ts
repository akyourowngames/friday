import { describe, it, expect, vi } from "vitest";
import {
	runAgentLoop,
	runAgentLoopContinue,
} from "../src/agent-loop.ts";
import {
	EventStream,
	AssistantMessageEventStream,
} from "../src/event-stream.ts";
import type {
	AgentContext,
	AgentEvent,
	AgentLoopConfig,
	AgentMessage,
	AssistantMessage,
	Tool,
} from "../src/types.ts";

function makeTool(): Tool {
	return {
		name: "echo",
		description: "Echo back the input",
		parameters: { type: "object" as const, properties: { msg: { type: "string" as const, description: "message" } }, required: ["msg"] } as any,
		execute: async (_id, params, _signal, onProgress) => {
			await onProgress?.({ content: [{ type: "text", text: `Running: ${params.msg}` }] });
			return {
				content: [{ type: "text", text: `Echo: ${params.msg}` }],
			};
		},
	};
}

function makeConfig(streamFn: any, tools?: Tool[]): AgentLoopConfig {
	return {
		model: { id: "faux-1", name: "Faux", api: "faux", provider: "faux", baseUrl: "", reasoning: false, input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 }, contextWindow: 4096, maxTokens: 2048 },
		streamFunction: streamFn,
		tools: tools ?? [makeTool()],
		convertToLlm: (msgs: AgentMessage[]) => msgs,
	};
}

function makeContext(tools?: Tool[]): AgentContext {
	return {
		systemPrompt: "You are a test agent.",
		messages: [],
		tools,
	};
}

describe("agentLoop", () => {
	it("should emit message_start, message_update, message_end events", async () => {
		const stream = new AssistantMessageEventStream();
		const msg: AssistantMessage = {
			role: "assistant",
			content: [{ type: "text", text: "Hello!" }],
			api: "faux",
			provider: "faux",
			model: "faux-1",
			usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
			stopReason: "stop",
			timestamp: Date.now(),
		};

		stream.push({ type: "start", partial: { ...msg, content: [] } });
		stream.push({ type: "text_delta", contentIndex: 0, delta: "Hello", partial: { ...msg, content: [{ type: "text", text: "Hello" }] } });
		stream.push({ type: "text_delta", contentIndex: 0, delta: "!", partial: msg });
		stream.push({ type: "done", reason: "stop", message: msg });

		const streamFn = vi.fn().mockReturnValue(stream);
		const config = makeConfig(streamFn);
		const context = makeContext();

		const events: AgentEvent[] = [];
		const emit = async (e: AgentEvent) => events.push(e);

		await runAgentLoop(
			[{ role: "user", content: "Hi", timestamp: Date.now() }],
			context,
			config,
			emit,
			new AbortController().signal,
			streamFn,
		);

		expect(events.some((e) => e.type === "message_start")).toBe(true);
		expect(events.some((e) => e.type === "message_update")).toBe(true);
		expect(events.some((e) => e.type === "message_end")).toBe(true);
	});

	it("should execute tools when assistant calls them", async () => {
		const stream = new AssistantMessageEventStream();

		const msg: AssistantMessage = {
			role: "assistant",
			content: [
				{ type: "toolCall", id: "call_1", name: "echo", arguments: { msg: "hello" } },
			],
			api: "faux",
			provider: "faux",
			model: "faux-1",
			usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
			stopReason: "toolUse",
			timestamp: Date.now(),
		};

		stream.push({ type: "start", partial: { ...msg, content: [] } });
		stream.push({ type: "done", reason: "toolUse", message: msg });

		// After the tool executes, the loop calls streamFn again for the next
		// turn. Serve a fresh, terminating "stop" response — reusing the same
		// (exhausted) stream would replay the toolUse message forever.
		const followUpMsg: AssistantMessage = {
			role: "assistant",
			content: [{ type: "text", text: "All done!" }],
			api: "faux",
			provider: "faux",
			model: "faux-1",
			usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
			stopReason: "stop",
			timestamp: Date.now(),
		};
		const followUpStream = new AssistantMessageEventStream();
		followUpStream.push({ type: "start", partial: { ...followUpMsg, content: [] } });
		followUpStream.push({ type: "done", reason: "stop", message: followUpMsg });

		const streamFn = vi.fn().mockReturnValueOnce(stream).mockReturnValueOnce(followUpStream);
		const config = makeConfig(streamFn);
		const context = makeContext([makeTool()]);

		const events: AgentEvent[] = [];
		const emit = async (e: AgentEvent) => events.push(e);

		await runAgentLoop(
			[{ role: "user", content: "Echo hello", timestamp: Date.now() }],
			context,
			config,
			emit,
			new AbortController().signal,
			streamFn,
		);

		expect(events.some((e) => e.type === "tool_execution_start")).toBe(true);
		const progress = events.find((e) => e.type === "tool_execution_progress");
		expect(progress).toMatchObject({
			type: "tool_execution_progress",
			toolCallId: "call_1",
			toolName: "echo",
			progress: { content: [{ type: "text", text: "Running: hello" }] },
		});
		expect(events.some((e) => e.type === "tool_execution_end")).toBe(true);
	});

	it("should abort streaming on abort signal", async () => {
		const controller = new AbortController();
		const stream = new AssistantMessageEventStream();

		// Simulate a well-behaved provider: when the signal aborts, it pushes an
		// aborted error event and terminates the stream (like provider-faux.ts).
		controller.signal.addEventListener("abort", () => {
			const aborted: AssistantMessage = {
				role: "assistant",
				content: [],
				api: "faux",
				provider: "faux",
				model: "faux-1",
				usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
				stopReason: "aborted",
				errorMessage: "Request was aborted",
				timestamp: Date.now(),
			};
			stream.push({ type: "error", reason: "aborted", error: aborted });
		});

		const streamFn = vi.fn().mockReturnValue(stream);
		const config = makeConfig(streamFn);
		const context = makeContext();

		const emit = vi.fn();

		const promise = runAgentLoop(
			[{ role: "user", content: "Hi", timestamp: Date.now() }],
			context,
			config,
			emit,
			controller.signal,
			streamFn,
		);

		controller.abort();
		await expect(promise).resolves.not.toThrow();
	});

	it("should handle error events from stream", async () => {
		const stream = new AssistantMessageEventStream();

		const streamFn = vi.fn().mockReturnValue(stream);
		const config = makeConfig(streamFn);
		const context = makeContext();

		const events: AgentEvent[] = [];
		const emit = async (e: AgentEvent) => events.push(e);

		// Push error event before the loop starts consuming
		const msg: AssistantMessage = {
			role: "assistant",
			content: [],
			api: "faux",
			provider: "faux",
			model: "faux-1",
			usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
			stopReason: "error",
			errorMessage: "Something broke",
			timestamp: Date.now(),
		};

		stream.push({ type: "error", reason: "error", error: msg });

		await runAgentLoop(
			[{ role: "user", content: "Hi", timestamp: Date.now() }],
			context,
			config,
			emit,
			new AbortController().signal,
			streamFn,
		);

		expect(events.some((e) => e.type === "message_end")).toBe(true);
	});
});

describe("agentLoopContinue", () => {
	it("should continue from a user message", async () => {
		const stream = new AssistantMessageEventStream();

		const msg: AssistantMessage = {
			role: "assistant",
			content: [{ type: "text", text: "OK" }],
			api: "faux",
			provider: "faux",
			model: "faux-1",
			usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
			stopReason: "stop",
			timestamp: Date.now(),
		};

		stream.push({ type: "start", partial: msg });
		stream.push({ type: "done", reason: "stop", message: msg });

		const streamFn = vi.fn().mockReturnValue(stream);
		const config = makeConfig(streamFn);
		const context: AgentContext = {
			systemPrompt: "test",
			messages: [{ role: "user", content: "Continue", timestamp: Date.now() }],
		};

		const events: AgentEvent[] = [];
		const emit = async (e: AgentEvent) => events.push(e);

		await runAgentLoopContinue(
			context,
			config,
			emit,
			new AbortController().signal,
			streamFn,
		);

		expect(streamFn).toHaveBeenCalled();
		expect(events.some((e) => e.type === "message_end")).toBe(true);
	});
});
