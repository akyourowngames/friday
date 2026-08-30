/**
 * End-to-end verification of the diff pipeline.
 *
 * Unit tests hand the renderers synthetic `details` objects. That can't catch
 * the failure mode that actually matters: the tools emitting one shape and the
 * renderers expecting another. So these tests run the *real* `write` / `edit` /
 * `multi_edit` tools against real files on disk, push the resulting
 * `tool_execution_end` events through the real agent loop and both renderers,
 * and assert on what a human would actually see.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import type { AgentEvent, AgentEventSink, Tool } from "../src/types.ts";

const { createOpenAICompatStreamFn } = await import("../src/providers/openai-compat.ts");
const { runAgentLoop } = await import("../src/agent-loop.ts");
const { writeTool, editTool, multiEditTool } = await import("../src/tools/shell.ts");
const { ConsoleRenderer } = await import("../src/console-renderer.ts");
const { renderToolEntry, summarizeToolResult, visibleWidth } = await import("../src/tui.ts");

let tmp = "";

beforeEach(async () => {
	tmp = await fs.mkdtemp(path.join(os.tmpdir(), "friday-diff-"));
});
afterEach(async () => {
	await fs.rm(tmp, { recursive: true, force: true });
});

function lines(count: number, prefix = "line"): string {
	return Array.from({ length: count }, (_, i) => `${prefix}-${i}`).join("\n");
}

/** Render a tool result through the console renderer, exactly as the CLI does. */
function consoleOutput(toolName: string, result: unknown, isError = false): string {
	const out: string[] = [];
	const renderer = new ConsoleRenderer({ out: (text) => out.push(text) });
	renderer.render({ type: "tool_execution_end", toolCallId: "t1", toolName, result: result as any, isError });
	return out.join("");
}

describe("real tool results render as diffs", () => {
	it("write: creates a file and shows a one-line diff, not the whole file", async () => {
		const file = path.join(tmp, "app.ts");
		const before = lines(40);
		const after = before.replace("line-20", "line-20-CHANGED");
		await fs.writeFile(file, before, "utf8");

		const result = await writeTool.execute("t1", { path: file, content: after, root: tmp });
		expect(result.isError).toBeFalsy();
		expect(await fs.readFile(file, "utf8")).toBe(after);

		const out = consoleOutput("write", result);
		expect(out).toContain("- line-20");
		expect(out).toContain("+ line-20-CHANGED");
		// Console renderer keeps 2 lines of context, so the hunk spans 19-23.
		expect(out).toContain("@@ -19,5 +19,5 @@");
		// 40-line file, one change: the other 37 lines must stay off screen.
		expect(out).not.toContain("line-0\n");
		expect(out).not.toContain("line-39");
		expect(out).not.toContain("\x1b[");

		// The TUI keeps 1 line of context, so the same edit reports a 3-line hunk.
		const box = renderToolEntry({ role: "tool", text: "", name: "write", args: {}, status: "done", result }, 80).join("\n");
		expect(box).toContain("@@ -20,3 +20,3 @@");
		expect(box).toContain("+ line-20-CHANGED");
	});

	it("write to a brand-new file reports a new file with no diff noise", async () => {
		const file = path.join(tmp, "fresh.ts");
		const result = await writeTool.execute("t1", { path: file, content: "one\ntwo\n", root: tmp });
		expect(summarizeToolResult("write", result, false)).toBe("wrote 8 bytes (new file)");
		expect(consoleOutput("write", result)).toContain("+ one");
	});

	it("edit: swapping one line shows exactly that line", async () => {
		const file = path.join(tmp, "edit.txt");
		await fs.writeFile(file, "alpha\nbeta\ngamma\n", "utf8");
		const result = await editTool.execute("t1", { path: file, oldText: "beta", newText: "BETA", root: tmp });
		expect(await fs.readFile(file, "utf8")).toBe("alpha\nBETA\ngamma\n");
		expect(summarizeToolResult("edit", result, false)).toBe("edited (+1 -1)");
		const out = consoleOutput("edit", result);
		expect(out).toContain("- beta");
		expect(out).toContain("+ BETA");
		expect(out).not.toContain("alpha");
	});

	it("multi_edit: labels each file it touched", async () => {
		const first = path.join(tmp, "a.txt");
		const second = path.join(tmp, "b.txt");
		await fs.writeFile(first, "alpha", "utf8");
		await fs.writeFile(second, "beta", "utf8");
		const result = await multiEditTool.execute("t1", {
			root: tmp,
			edits: [
				{ path: first, oldText: "alpha", newText: "ALPHA" },
				{ path: second, oldText: "beta", newText: "BETA" },
			],
		});
		expect(result.isError).toBeFalsy();
		const out = consoleOutput("multi_edit", result);
		expect(out).toContain(first);
		expect(out).toContain(second);
		expect(out).toContain("+ ALPHA");
		expect(out).toContain("+ BETA");
		expect(summarizeToolResult("multi_edit", result, false)).toBe("2 edits across 2 files (+2 -2)");
	});

	it("oversized writes degrade to a byte summary instead of a diff", async () => {
		const file = path.join(tmp, "huge.txt");
		const before = "x".repeat(300 * 1024);
		await fs.writeFile(file, before, "utf8");
		const result = await writeTool.execute("t1", { path: file, content: `${before}y`, root: tmp });
		expect((result.details as any).diffTooLarge).toBe(true);
		expect((result.details as any).oldText).toBeUndefined();
		expect(summarizeToolResult("write", result, false)).toBe(`wrote ${before.length + 1} bytes`);
		expect(consoleOutput("write", result)).not.toContain("@@");
	});

	it("renders nothing for tools that have no diff", async () => {
		const result = await editTool.execute("t1", { path: path.join(tmp, "nope.txt"), oldText: "a", newText: "b", root: tmp });
		expect(result.isError).toBe(true);
		expect(consoleOutput("edit", result, true)).not.toContain("- a");
	});
});

describe("TUI box safety with real diffs", () => {
	it("never overflows the box width, even with long lines and CJK", async () => {
		const file = path.join(tmp, "wide.ts");
		const long = `const value = "${"x".repeat(300)}";`;
		const before = `${long}\nconst 中文 = "日本語のテキスト";\nshort`;
		const after = before.replace("short", "SHORT");
		await fs.writeFile(file, before, "utf8");
		const result = await writeTool.execute("t1", { path: file, content: after, root: tmp });

		for (const width of [40, 60, 90]) {
			const rendered = renderToolEntry(
				{ role: "tool", text: "", name: "write", args: { path: "wide.ts" }, status: "done", result },
				width,
			);
			for (const line of rendered) {
				expect(visibleWidth(line)).toBeLessThanOrEqual(width);
			}
		}
		// The change itself is still visible at a normal width.
		expect(renderToolEntry({ role: "tool", text: "", name: "write", args: {}, status: "done", result }, 90).join("\n")).toContain("+ SHORT");
	});

	it("stays inside its 12-line budget for a large multi-hunk write", async () => {
		const file = path.join(tmp, "big.ts");
		const before = lines(400);
		let after = before;
		for (const n of [10, 100, 200, 300, 390]) after = after.replace(`line-${n}\n`, `line-${n}-edited\n`);
		await fs.writeFile(file, before, "utf8");
		const result = await writeTool.execute("t1", { path: file, content: after, root: tmp });
		const body = renderToolEntry({ role: "tool", text: "", name: "write", args: {}, status: "done", result }, 80);
		// Box border + title + summary + up to 12 diff lines + closing border.
		expect(body.length).toBeLessThanOrEqual(3 + 12 + 1);
	});
});

describe("the agent loop carries the diff payload through untouched", () => {
	const testModel = {
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
	} as any;

	let originalFetch: typeof globalThis.fetch;

	beforeEach(() => {
		originalFetch = globalThis.fetch;
	});
	afterEach(() => {
		globalThis.fetch = originalFetch;
		vi.restoreAllMocks();
	});

	function sse(choice: Record<string, unknown>): string {
		return `data: ${JSON.stringify({ id: "x", object: "chat.completion.chunk", model: "test-model", choices: [{ index: 0, ...choice }] })}\n\n`;
	}

	function scriptedFetch(responses: string[][]): typeof globalThis.fetch {
		let call = 0;
		return vi.fn(async (_input: any, _init?: any) => {
			const next = responses[Math.min(call, responses.length - 1)]!;
			call++;
			const body = `${next.join("")}data: [DONE]\n\n`;
			return new Response(
				new ReadableStream<Uint8Array>({
					start(controller) {
						controller.enqueue(new TextEncoder().encode(body));
						controller.close();
					},
				}),
				{ status: 200, headers: { "content-type": "text/event-stream" } },
			);
		}) as unknown as typeof globalThis.fetch;
	}

	it("a real write through runAgentLoop prints a diff in the console renderer", async () => {
		const file = path.join(tmp, "loop.ts");
		const before = lines(30);
		const after = before.replace("line-15", "line-15-FIXED");
		await fs.writeFile(file, before, "utf8");

		const args = JSON.stringify({ path: file, content: after, root: tmp });
		globalThis.fetch = scriptedFetch([
			[
				sse({ delta: { role: "assistant", tool_calls: [{ index: 0, id: "call_1", type: "function", function: { name: "write", arguments: "" } }] }, finish_reason: null }),
				sse({ delta: { tool_calls: [{ index: 0, function: { arguments: args } }] }, finish_reason: "tool_calls" }),
			],
			[sse({ delta: { content: "patched" }, finish_reason: null }), sse({ delta: {}, finish_reason: "stop" })],
		]);

		const streamFn = createOpenAICompatStreamFn({ model: "test-model", apiKey: "sk-test", baseUrl: "http://localhost:9999/v1" });
		const events: AgentEvent[] = [];
		const emit: AgentEventSink = async (e) => events.push(e);
		await runAgentLoop(
			[{ role: "user", content: "patch it", timestamp: Date.now() }],
			{ systemPrompt: "test", messages: [], tools: [writeTool as Tool] },
			{ model: testModel, convertToLlm: (m: any[]) => m, streamFunction: streamFn, toolExecution: "sequential" } as any,
			emit,
			undefined,
			streamFn,
		);

		const end = events.find((e) => e.type === "tool_execution_end" && e.toolName === "write");
		expect(end).toBeDefined();

		// Replay the captured event through the renderer: the diff survives the
		// whole trip from tool result → agent event → terminal.
		const out: string[] = [];
		const renderer = new ConsoleRenderer({ out: (text) => out.push(text) });
		renderer.render(end!);
		expect(out.join("")).toContain("+ line-15-FIXED");
		expect(await fs.readFile(file, "utf8")).toBe(after);
	});
});
