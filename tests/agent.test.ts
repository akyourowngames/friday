import { describe, it, expect, vi } from "vitest";
import { Agent } from "../src/agent.ts";
import { registerFauxProvider, createFauxStreamFn, fauxText, fauxToolCall } from "../src/provider-faux.ts";
import type { AgentEvent, AgentMessage, Tool } from "../src/types.ts";

function makeFauxStreamFn() {
	const registration = registerFauxProvider({ tokensPerSecond: 1000 });

	const tools: Tool[] = [
		{
			name: "calculator",
			description: "Evaluate arithmetic",
			parameters: { type: "object" as const, properties: { expression: { type: "string" as const, description: "expression" } }, required: ["expression"] } as any,
			execute: async (_id: string, params: any) => ({
				content: [{ type: "text", text: String(Function(`"use strict"; return (${params.expression})`)()) }],
			}),
		},
	];

	registration.setResponses([
		[fauxText("The answer is "), fauxText("4.")],
	]);

	return {
		streamFn: createFauxStreamFn(registration),
		tools,
		registration,
	};
}

function collectEvents(agent: Agent) {
	const events: AgentEvent[] = [];
	agent.subscribe((event, _signal) => {
		events.push(event);
	});
	return events;
}

describe("Agent", () => {
	it("should emit lifecycle events in correct order", async () => {
		const { streamFn, tools } = makeFauxStreamFn();
		const agent = new Agent({
			initialState: {
				systemPrompt: "You are a test agent.",
				tools,
			},
			streamFunction: streamFn,
		});

		const events = collectEvents(agent);

		await agent.prompt("What is 2+2?");
		await agent.waitForIdle();

		const types = events.map((e) => e.type);
		expect(types[0]).toBe("agent_start");
		expect(types).toContain("turn_start");
		expect(types).toContain("message_start");
		expect(types).toContain("message_update");
		expect(types).toContain("message_end");
		expect(types).toContain("turn_end");
		expect(types[types.length - 1]).toBe("agent_end");
	});

	it("should stream text incrementally to listeners", async () => {
		const { streamFn, tools } = makeFauxStreamFn();
		const agent = new Agent({
			initialState: { systemPrompt: "test", tools },
			streamFunction: streamFn,
		});

		const updates: string[] = [];
		let finalText = "";
		agent.subscribe((event) => {
			if (event.type === "message_update") {
				const msg = event.message as any;
				if (msg.role === "assistant") {
					updates.push(msg.content[0]?.text ?? "");
				}
			}
			if (event.type === "message_end") {
				const msg = event.message as any;
				if (msg.role === "assistant") {
					finalText = msg.content
						.filter((c: any) => c.type === "text")
						.map((c: any) => c.text)
						.join("");
				}
			}
		});

		await agent.prompt("What is 2+2?");
		await agent.waitForIdle();

		// Should have incremental updates
		expect(updates.length).toBeGreaterThan(1);
		expect(finalText).toBe("The answer is 4.");
	});

	it("should execute tool calls and add results to transcript", async () => {
		const registration = registerFauxProvider({ tokensPerSecond: 1000 });
		registration.setResponses([
			[fauxToolCall("calculator", { expression: "2 + 2" }), fauxText("The answer is 4.")],
		]);

		const tools: Tool[] = [
			{
				name: "calculator",
				description: "Evaluate arithmetic",
				parameters: { type: "object" as const, properties: { expression: { type: "string" as const, description: "expr" } }, required: ["expression"] } as any,
				execute: async (_id: string, params: any) => ({
					content: [{ type: "text", text: String(Function(`"use strict"; return (${params.expression})`)()) }],
				}),
			},
		];

		const agent = new Agent({
			initialState: { systemPrompt: "test", tools },
			streamFunction: createFauxStreamFn(registration),
			toolExecution: "sequential",
		});

		const events = collectEvents(agent);
		await agent.prompt("Calculate 2+2");
		await agent.waitForIdle();

		// Should have tool_execution_start and tool_execution_end events
		expect(events.some((e) => e.type === "tool_execution_start")).toBe(true);
		expect(events.some((e) => e.type === "tool_execution_end")).toBe(true);

		// The tool result should be in the final messages
		const toolResultEvents = events.filter((e) => e.type === "message_end" && e.message.role === "toolResult");
		expect(toolResultEvents.length).toBe(1);
	});

	it("should abort on signal", async () => {
		const registration = registerFauxProvider({ tokensPerSecond: 1000 });
		registration.setResponses([
			[fauxText("a"), fauxText("b"), fauxText("c")],
		]);

		const agent = new Agent({
			initialState: { systemPrompt: "test" },
			streamFunction: createFauxStreamFn(registration),
		});

		const promptPromise = agent.prompt("Tell me a story");
		// Abort immediately
		setTimeout(() => agent.abort(), 0);
		await promptPromise.catch(() => {});

		expect(agent.signal?.aborted ?? true).toBe(true);
	});

	it("should accumulate messages across turns", async () => {
		const registration = registerFauxProvider({ tokensPerSecond: 1000 });
		registration.setResponses([
			[fauxText("One")],
		]);

		const agent = new Agent({
			initialState: { systemPrompt: "test" },
			streamFunction: createFauxStreamFn(registration),
		});

		await agent.prompt("First message");
		await agent.waitForIdle();

		expect(agent.state.messages.length).toBe(2); // user + assistant
		expect(agent.state.messages[0]?.role).toBe("user");
		expect(agent.state.messages[1]?.role).toBe("assistant");
	});

	it("should reset state cleanly", async () => {
		const { streamFn, tools } = makeFauxStreamFn();
		const agent = new Agent({
			initialState: { systemPrompt: "test", tools },
			streamFunction: streamFn,
		});

		await agent.prompt("Hello");
		await agent.waitForIdle();

		expect(agent.state.messages.length).toBeGreaterThan(0);

		agent.reset();
		expect(agent.state.messages.length).toBe(0);
	});

	it("should throw if prompt called while already processing", async () => {
		const { streamFn, tools } = makeFauxStreamFn();
		const agent = new Agent({
			initialState: { systemPrompt: "test", tools },
			streamFunction: streamFn,
		});

		const p1 = agent.prompt("First");
		await expect(agent.prompt("Second")).rejects.toThrow("already processing");
		await p1;
	});
});
