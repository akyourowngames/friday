import { describe, it, expect } from "vitest";
import { ConsoleRenderer } from "../src/console-renderer.ts";
import { Agent } from "../src/agent.ts";
import { registerFauxProvider, createFauxStreamFn, fauxText } from "../src/provider-faux.ts";
import type { AgentEvent } from "../src/types.ts";

function makeBaseMessage() {
	return {
		role: "assistant" as const,
		content: [],
		api: "faux" as const,
		provider: "faux" as const,
		model: "faux-1",
		usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
		stopReason: "stop" as const,
		timestamp: 0,
	};
}

describe("ConsoleRenderer", () => {
	it("should render user messages", () => {
		const output: string[] = [];
		const renderer = new ConsoleRenderer({ out: (text) => output.push(text) });

		renderer.render({
			type: "message_start",
			message: { role: "user", content: "Hello agent", timestamp: 0 },
		});

		expect(output.some((o) => o.includes("You:"))).toBe(true);
		expect(output.some((o) => o.includes("Hello agent"))).toBe(true);
	});

	it("should render streaming assistant text incrementally", () => {
		const output: string[] = [];
		const renderer = new ConsoleRenderer({ out: (text) => output.push(text) });

		const base = makeBaseMessage();

		renderer.render({ type: "message_start", message: base });
		renderer.render({ type: "message_update", message: { ...base, content: [{ type: "text", text: "Hel" }] }, assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "Hel", partial: base } });
		renderer.render({ type: "message_update", message: { ...base, content: [{ type: "text", text: "Hello" }] }, assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "lo", partial: base } });
		renderer.render({ type: "message_end", message: { ...base, content: [{ type: "text", text: "Hello" }] } });

		const finalOutput = output.join("");
		expect(finalOutput).toContain("Hello");
	});

	it("should render thinking blocks when showThinking is true", () => {
		const output: string[] = [];
		const renderer = new ConsoleRenderer({ out: (text) => output.push(text), showThinking: true });

		const base = makeBaseMessage();
		const msgWithThinking = { ...base, content: [{ type: "thinking" as const, thinking: "Let me think..." }] };

		renderer.render({ type: "message_start", message: base });
		renderer.render({ type: "message_update", message: msgWithThinking, assistantMessageEvent: { type: "thinking_delta", contentIndex: 0, delta: "...", partial: msgWithThinking } });

		const finalOutput = output.join("");
		expect(finalOutput).toContain("[thinking:");
	});

	it("should hide thinking blocks when showThinking is false", () => {
		const output: string[] = [];
		const renderer = new ConsoleRenderer({ out: (text) => output.push(text), showThinking: false });

		const base = makeBaseMessage();
		const msgWithThinking = { ...base, content: [{ type: "thinking" as const, thinking: "Let me think..." }] };

		renderer.render({ type: "message_start", message: base });
		renderer.render({ type: "message_update", message: msgWithThinking, assistantMessageEvent: { type: "thinking_delta", contentIndex: 0, delta: "...", partial: msgWithThinking } });

		const finalOutput = output.join("");
		expect(finalOutput).not.toContain("Let me think");
	});

	it("should render tool execution events", () => {
		const output: string[] = [];
		const renderer = new ConsoleRenderer({ out: (text) => output.push(text) });

		renderer.render({ type: "tool_execution_start", toolCallId: "abc", toolName: "calculator", args: { expression: "2+2" } });
		renderer.render({ type: "tool_execution_end", toolCallId: "abc", toolName: "calculator", result: { content: [{ type: "text", text: "4" }] }, isError: false });

		const finalOutput = output.join("");
		expect(finalOutput).toContain("calculator");
		expect(finalOutput).toContain("4");
	});

	it("should render agent_end", () => {
		const output: string[] = [];
		const renderer = new ConsoleRenderer({ out: (text) => output.push(text) });

		renderer.render({ type: "agent_start" });
		renderer.render({ type: "agent_end", messages: [] });

		expect(output.join("")).toContain("Done.");
	});
});

describe("attachConsoleRenderer", () => {
	it("should subscribe renderer to agent events", async () => {
		const registration = registerFauxProvider({ tokensPerSecond: 1000 });
		registration.setResponses([[fauxText("Hello")]]);

		const agent = new Agent({
			initialState: { systemPrompt: "test" },
			streamFunction: createFauxStreamFn(registration),
		});

		const output: string[] = [];
		const renderer = new ConsoleRenderer({ out: (text) => output.push(text) });
		agent.subscribe((event: AgentEvent) => renderer.render(event));

		await agent.prompt("Hi");
		await agent.waitForIdle();

		expect(output.length).toBeGreaterThan(0);
		expect(output.join("")).toContain("Hello");
	});
});
