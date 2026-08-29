import { describe, it, expect } from "vitest";
import { HookRegistry } from "../src/hooks.ts";
import type { AgentTool, ToolResult } from "../src/types.ts";

const fakeTool = (): AgentTool<any, any> => ({
	name: "x",
	description: "x",
	parameters: {} as any,
	execute: async () => ({ output: "ok" }),
});

const fakeResult = (): ToolResult => ({ output: "ok" });

describe("HookRegistry", () => {
	it("runs hooks in registration order", async () => {
		const r = new HookRegistry();
		const calls: string[] = [];
		r.on("pre_tool_use", (p) => {
			calls.push("a");
		});
		r.on("pre_tool_use", (p) => {
			calls.push("b");
		});
		await r.trigger("pre_tool_use", { tool: fakeTool(), args: {}, callId: "1" });
		expect(calls).toEqual(["a", "b"]);
	});

	it("stops further hooks when one cancels", async () => {
		const r = new HookRegistry();
		const calls: string[] = [];
		r.on("pre_tool_use", () => {
			calls.push("a");
		});
		r.on("pre_tool_use", () => {
			calls.push("b");
			const r: any = { tool: undefined, args: undefined, callId: "", cancel: true, reason: "nope" };
			return r;
		});
		r.on("pre_tool_use", () => {
			calls.push("c");
		});
		const result = await r.trigger("pre_tool_use", { tool: fakeTool(), args: {}, callId: "1" });
		expect(calls).toEqual(["a", "b"]);
		expect(result.cancel).toBe(true);
		expect(result.reason).toBe("nope");
	});

	it("lets hooks transform the payload", async () => {
		const r = new HookRegistry();
		r.on("post_tool_use", (p) => ({ ...p, durationMs: 42 }));
		const result = await r.trigger("post_tool_use", {
			tool: fakeTool(),
			args: {},
			callId: "1",
			result: fakeResult(),
			durationMs: 0,
		});
		expect(result.durationMs).toBe(42);
	});

	it("supports async hooks", async () => {
		const r = new HookRegistry();
		let resolved = false;
		r.on("turn_end", async () => {
			await new Promise((res) => setTimeout(res, 5));
			resolved = true;
		});
		await r.trigger("turn_end", { messages: [], stopReason: "complete" });
		expect(resolved).toBe(true);
	});

	it("returns the unregister function", async () => {
		const r = new HookRegistry();
		const off = r.on("pre_tool_use", () => {});
		expect(r.count("pre_tool_use")).toBe(1);
		off();
		expect(r.count("pre_tool_use")).toBe(0);
	});

	it("clear removes every hook", async () => {
		const r = new HookRegistry();
		r.on("pre_tool_use", () => {});
		r.on("post_tool_use", () => {});
		r.clear();
		expect(r.count("pre_tool_use")).toBe(0);
		expect(r.count("post_tool_use")).toBe(0);
	});

	it("keeps independent state per event", async () => {
		const r = new HookRegistry();
		let aFired = false;
		let bFired = false;
		r.on("pre_tool_use", () => {
			aFired = true;
		});
		r.on("post_tool_use", () => {
			bFired = true;
		});
		await r.trigger("pre_tool_use", { tool: fakeTool(), args: {}, callId: "1" });
		expect(aFired).toBe(true);
		expect(bFired).toBe(false);
	});

	it("handles a no-op event without registered hooks", async () => {
		const r = new HookRegistry();
		const result = await r.trigger("turn_end", { messages: [], stopReason: "complete" });
		expect(result.stopReason).toBe("complete");
		expect(result.cancel).toBeUndefined();
	});
});
