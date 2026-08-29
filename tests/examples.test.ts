import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, writeFileSync, mkdirSync, copyFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { HookRegistry } from "../src/hooks.ts";
import { buildHost, loadExtensions } from "../src/extension-loader.ts";
import { listSlashCommands, clearSlashCommands } from "../src/slash-commands.ts";

const EXAMPLES = join(import.meta.dirname, "..", "examples", "extensions");

describe("example extensions", () => {
	let dir: string;
	beforeEach(() => {
		dir = mkdtempSync(join(tmpdir(), "friday-examples-"));
		clearSlashCommands();
	});
	afterEach(() => rmSync(dir, { recursive: true, force: true }));

	it("hello-world.js registers a /hello command", async () => {
		copyFileSync(join(EXAMPLES, "hello-world.js"), join(dir, "hello-world.js"));
		const hooks = new HookRegistry();
		const host = buildHost({
			hooks,
			getSetting: () => undefined,
			setSetting: () => {},
			log: () => {},
		});
		const result = await loadExtensions(dir, host);
		expect(result.loaded.length).toBe(1);
		const cmds = listSlashCommands();
		const hello = cmds.find((c) => c.name === "/hello");
		expect(hello).toBeDefined();
		expect(hello?.description).toContain("extension");
	});

	it("safe-rm.js registers a pre_tool_use veto", async () => {
		copyFileSync(join(EXAMPLES, "safe-rm.js"), join(dir, "safe-rm.js"));
		const hooks = new HookRegistry();
		let log: string[] = [];
		const host = buildHost({
			hooks,
			getSetting: () => undefined,
			setSetting: () => {},
			log: (m) => log.push(m),
		});
		await loadExtensions(dir, host);
		expect(hooks.count("pre_tool_use")).toBe(1);
		// Run the hook with a destructive command and expect a cancel.
		const result = await hooks.trigger("pre_tool_use", {
			tool: { name: "bash", description: "", parameters: {} as any, execute: async () => ({ output: "" }) },
			args: { command: "rm -rf /" },
			callId: "x",
		});
		expect(result.cancel).toBe(true);
		expect(result.reason).toContain("rm -rf");
		// And a safe command should not be vetoed.
		const safe = await hooks.trigger("pre_tool_use", {
			tool: { name: "bash", description: "", parameters: {} as any, execute: async () => ({ output: "" }) },
			args: { command: "ls" },
			callId: "y",
		});
		expect(safe.cancel).toBeUndefined();
	});
});
