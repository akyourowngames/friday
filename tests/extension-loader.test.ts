import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { HookRegistry } from "../src/hooks.ts";
import {
	buildHost,
	defaultExtensionsDir,
	discoverExtensions,
	loadExtensions,
} from "../src/extension-loader.ts";
import { clearSlashCommands, listSlashCommands, registerSlashCommand } from "../src/slash-commands.ts";
import { mkdtempSync, writeFileSync, rmSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

describe("buildHost", () => {
	it("returns a host with the expected API surface", () => {
		const hooks = new HookRegistry();
		const settings: Record<string, unknown> = {};
		const host = buildHost({
			hooks,
			getSetting: (k) => settings[k],
			setSetting: (k, v) => {
				settings[k] = v;
			},
			log: () => {},
		});
		expect(typeof host.commands.register).toBe("function");
		expect(typeof host.commands.list).toBe("function");
		expect(typeof host.commands.get).toBe("function");
		expect(host.hooks).toBe(hooks);
		expect(host.getSetting("x")).toBeUndefined();
		host.setSetting("x", 1);
		expect(settings.x).toBe(1);
	});
});

describe("discoverExtensions", () => {
	let dir: string;
	beforeEach(() => {
		dir = mkdtempSync(join(tmpdir(), "friday-ext-"));
	});
	it("returns an empty list for an empty directory", () => {
		expect(discoverExtensions(dir)).toEqual([]);
	});
	it("finds .js files at the top level", () => {
		writeFileSync(join(dir, "a.js"), "export default () => {};");
		writeFileSync(join(dir, "b.mjs"), "export default () => {};");
		expect(discoverExtensions(dir).sort()).toEqual([join(dir, "a.js"), join(dir, "b.mjs")]);
	});
	it("recurses into subdirectories", () => {
		writeFileSync(join(dir, "a.js"), "");
		mkdirSync(join(dir, "sub"));
		writeFileSync(join(dir, "sub", "b.js"), "");
		expect(discoverExtensions(dir).sort()).toEqual([join(dir, "a.js"), join(dir, "sub", "b.js")]);
	});
	it("ignores non-code files", () => {
		writeFileSync(join(dir, "a.js"), "");
		writeFileSync(join(dir, "README.md"), "");
		writeFileSync(join(dir, "x.json"), "");
		expect(discoverExtensions(dir)).toEqual([join(dir, "a.js")]);
	});
});

describe("loadExtensions", () => {
	let dir: string;
	beforeEach(() => {
		dir = mkdtempSync(join(tmpdir(), "friday-ext-"));
		clearSlashCommands();
	});
	afterEach(() => rmSync(dir, { recursive: true, force: true }));

	it("loads and runs an extension", async () => {
		const ext = join(dir, "myext.js");
		writeFileSync(
			ext,
			`export default (host) => {
				host.commands.register({
					name: "/hello",
					description: "hi",
					run: () => ({ message: "hi!" }),
				});
			};`,
		);
		const hooks = new HookRegistry();
		const host = buildHost({
			hooks,
			getSetting: () => undefined,
			setSetting: () => {},
			log: () => {},
		});
		const result = await loadExtensions(dir, host);
		expect(result.loaded).toEqual([ext]);
		expect(result.failed).toEqual([]);
		const cmds = listSlashCommands();
		expect(cmds.some((c) => c.name === "/hello")).toBe(true);
	});

	it("captures errors from broken extensions", async () => {
		const ext = join(dir, "broken.js");
		writeFileSync(ext, `throw new Error("nope");`);
		const hooks = new HookRegistry();
		const host = buildHost({
			hooks,
			getSetting: () => undefined,
			setSetting: () => {},
			log: () => {},
		});
		const result = await loadExtensions(dir, host);
		expect(result.loaded).toEqual([]);
		expect(result.failed.length).toBe(1);
		expect(result.failed[0].file).toBe(ext);
		expect(result.failed[0].error.message).toContain("nope");
	});

	it("returns an empty result for a non-existent directory", async () => {
		const hooks = new HookRegistry();
		const host = buildHost({
			hooks,
			getSetting: () => undefined,
			setSetting: () => {},
			log: () => {},
		});
		const result = await loadExtensions("/no/such/path", host);
		expect(result.loaded).toEqual([]);
		expect(result.failed).toEqual([]);
	});
});

describe("defaultExtensionsDir", () => {
	it("uses $HOME / $USERPROFILE", () => {
		process.env.HOME = "/x/y";
		process.env.USERPROFILE = "/x/y";
		const dir = defaultExtensionsDir();
		// Path is platform-dependent; check the trailing suffix.
		expect(dir.replace(/\\/g, "/")).toBe("/x/y/.friday-ng/extensions");
	});
});
