import { describe, it, expect } from "vitest";
import { ConsoleRenderer, wrapToWidth } from "../src/console-renderer.ts";
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
		expect(finalOutput).toContain("(thinking)");
	});

	it("rewinds and clears before rewriting (no carriage-return-only duplication)", () => {
		const output: string[] = [];
		const renderer = new ConsoleRenderer({ out: (text) => output.push(text) });

		const base = makeBaseMessage();
		renderer.render({ type: "message_start", message: base });
		renderer.render({ type: "message_update", message: { ...base, content: [{ type: "text", text: "short" }] }, assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "short", partial: base } });
		output.length = 0;
		renderer.render({ type: "message_update", message: { ...base, content: [{ type: "text", text: "short and now longer" }] }, assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "er", partial: base } });

		const screen = output.join("");
		// The fix rewinds + clears to end-of-screen (instead of a bare \r), which
		// is what prevents wrapped text from being duplicated.
		expect(screen).toContain("\x1b[J");
		expect(screen).toContain("short and now longer");
	});

	it("does not repaint identical text (idempotent)", () => {
		const output: string[] = [];
		const renderer = new ConsoleRenderer({ out: (text) => output.push(text) });

		const base = makeBaseMessage();
		renderer.render({ type: "message_start", message: base });
		renderer.render({ type: "message_update", message: { ...base, content: [{ type: "text", text: "same" }] }, assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "same", partial: base } });
		output.length = 0;
		renderer.render({ type: "message_update", message: { ...base, content: [{ type: "text", text: "same" }] }, assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "", partial: base } });

		expect(output.join("")).toBe("");
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

	it("prints a plain-text diff after a file write", () => {
		const output: string[] = [];
		const renderer = new ConsoleRenderer({ out: (text) => output.push(text) });
		const before = Array.from({ length: 20 }, (_, i) => `line-${i}`).join("\n");
		const after = before.replace("line-10", "changed");

		renderer.render({
			type: "tool_execution_end",
			toolCallId: "abc",
			toolName: "write",
			result: { content: [{ type: "text", text: "Wrote 132 bytes" }], details: { path: "f.ts", oldText: before, newText: after } },
			isError: false,
		});

		const finalOutput = output.join("");
		expect(finalOutput).toContain("- line-10");
		expect(finalOutput).toContain("+ changed");
		// Two lines of context on each side, so the hunk spans old lines 9-13.
		expect(finalOutput).toContain("@@ -9,5 +9,5 @@");
		expect(finalOutput).toContain("⋮ 8 unchanged lines");
		// No ANSI escapes — this renderer is routinely piped to files.
		expect(finalOutput).not.toContain("\x1b[");
	});

	it("leaves non-file tools and errors alone", () => {
		const output: string[] = [];
		const renderer = new ConsoleRenderer({ out: (text) => output.push(text) });
		renderer.render({ type: "tool_execution_end", toolCallId: "a", toolName: "bash", result: { content: [{ type: "text", text: "ok" }], details: { code: 0 } }, isError: false });
		renderer.render({ type: "tool_execution_end", toolCallId: "b", toolName: "edit", result: { content: [{ type: "text", text: "boom" }], details: { oldText: "a", newText: "b" } }, isError: true });
		expect(output.join("")).not.toContain("- a");
	});

	it("can be switched off", () => {
		const output: string[] = [];
		const renderer = new ConsoleRenderer({ out: (text) => output.push(text), showDiffs: false });
		renderer.render({ type: "tool_execution_end", toolCallId: "a", toolName: "edit", result: { content: [], details: { oldText: "a", newText: "b" } }, isError: false });
		expect(output.join("")).not.toContain("- a");
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

describe("wrapToWidth", () => {
	it("wraps long single-line text onto multiple rows", () => {
		const lines = wrapToWidth("the quick brown fox jumps over the lazy dog", 10);
		expect(lines.length).toBeGreaterThan(1);
		for (const l of lines) expect(l.length).toBeLessThanOrEqual(10);
	});

	it("preserves explicit newlines", () => {
		expect(wrapToWidth("a\nb", 10)).toEqual(["a", "b"]);
	});
});
