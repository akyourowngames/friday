import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
	bashTool,
	readTool,
	writeTool,
	editTool,
	multiEditTool,
	globTool,
	grepTool,
	builtinTools,
	isDangerousShellCommand,
} from "../src/tools/shell.ts";
import { isPathInside, resolveSafePath } from "../src/tools/path-safety.ts";

let tmp: string;
beforeEach(async () => {
	tmp = path.join(os.tmpdir(), `friday-ng-tools-${Date.now()}-${Math.random()}`);
	await fs.mkdir(tmp, { recursive: true });
});
afterEach(async () => {
	await fs.rm(tmp, { recursive: true, force: true });
});

describe("path-safety", () => {
	it("isPathInside returns true for descendant paths", () => {
		const root = path.resolve("/a");
		const child = path.join(root, "b", "c");
		expect(isPathInside(root, child)).toBe(true);
		expect(isPathInside(root, root)).toBe(true);
	});
	it("isPathInside returns false for ascendant or unrelated paths", () => {
		const root = path.resolve("/a");
		expect(isPathInside(root, path.resolve("/b"))).toBe(false);
		// Sibling dir that starts with the same prefix
		const sibling = path.resolve(path.dirname(root), path.basename(root) + "-b", "c");
		expect(isPathInside(root, sibling)).toBe(false);
	});
	it("resolveSafePath joins relative paths under root", () => {
		const root = path.resolve("/root");
		expect(resolveSafePath(root, "foo.txt")).toBe(path.join(root, "foo.txt"));
	});
	it("resolveSafePath returns absolute paths as-is", () => {
		const abs = path.resolve("/etc/passwd");
		expect(resolveSafePath(path.resolve("/root"), abs)).toBe(abs);
	});
});

describe("bashTool", () => {
	it("runs a simple command and returns exit code 0", async () => {
		const result = await bashTool.execute("t1", { command: process.platform === "win32" ? "echo hi" : "echo hi" });
		expect(result.isError).toBeFalsy();
		const text = (result.content[0] as any).text;
		expect(text).toContain("hi");
		expect(text).toContain("exit code 0");
	});
	it("rejects obviously dangerous commands", async () => {
		expect(isDangerousShellCommand("rm -rf /")).toBe(true);
		expect(isDangerousShellCommand("echo safe")).toBe(false);
		const r = await bashTool.execute("t1", { command: "rm -rf /" });
		expect(r.isError).toBe(true);
	});
	it("streams stdout and stderr while retaining final output", async () => {
		const command = process.platform === "win32" ? "echo out & echo err 1>&2" : "printf out; printf err >&2";
		const progress: any[] = [];
		const r = await bashTool.execute("t1", { command }, undefined, (update) => progress.push(update));
		expect(progress.some((update) => update.details?.stream === "stdout")).toBe(true);
		expect(progress.some((update) => update.details?.stream === "stderr")).toBe(true);
		const text = (r.content[0] as any).text;
		expect(text).toContain("out");
		expect(text).toContain("err");
	});
	it("captures non-zero exit codes without isError", async () => {
		const cmd = process.platform === "win32" ? "exit 7" : "exit 7";
		const r = await bashTool.execute("t1", { command: cmd });
		expect(r.isError).toBeFalsy();
		const text = (r.content[0] as any).text;
		// Either "code 7" or "(command exited with code 7 and no output)"
		expect(text).toMatch(/code 7/);
	});
	it("gives stdin-reading commands immediate EOF instead of hanging (the `date` hang bug)", async () => {
		// Windows: bare `date` prompts for input; with stdin closed it must
		// return promptly instead of blocking until the 60s timeout.
		// POSIX: `cat` with no args reads stdin; EOF ends it immediately.
		const command = process.platform === "win32" ? "date" : "cat";
		const start = Date.now();
		const r = await bashTool.execute("t1", { command, timeoutMs: 10_000 });
		const elapsed = Date.now() - start;
		expect(elapsed).toBeLessThan(8000);
		expect(r.isError).toBeFalsy();
		const details = (r as any).details;
		expect(details?.timedOut).toBe(false);
	}, 15_000);
	it("reports a timeout instead of returning a misleading exit code 0", async () => {
		const sleep = process.platform === "win32" ? "ping -n 15 127.0.0.1 >nul" : "sleep 15";
		const r = await bashTool.execute("t1", { command: sleep, timeoutMs: 1000 });
		const text = (r.content[0] as any).text;
		expect(text).toContain("timed out after 1s");
		expect((r as any).details?.timedOut).toBe(true);
	}, 10_000);
	it("description tells the model which shell and OS it is on", async () => {
		const desc: string = (bashTool as any).description;
		if (process.platform === "win32") {
			expect(desc).toContain("cmd.exe");
			expect(desc).toContain("NOT a POSIX shell");
		} else {
			expect(desc).toContain("/bin/sh");
		}
	});
});

describe("readTool / writeTool / editTool", () => {
	it("write + read round-trip", async () => {
		const file = path.join(tmp, "hello.txt");
		const w = await writeTool.execute("t1", { path: file, content: "hi there", root: tmp });
		expect(w.isError).toBeFalsy();
		expect(w.details).toMatchObject({ path: file, oldText: "", newText: "hi there" });
		const r = await readTool.execute("t1", { path: file, root: tmp });
		expect((r.content[0] as any).text).toContain("hi there");
	});
	it("reads supported images as base64 content", async () => {
		const file = path.join(tmp, "pixel.png");
		const bytes = Buffer.from([0x89, 0x50, 0x4e, 0x47]);
		await fs.writeFile(file, bytes);
		const r = await readTool.execute("t1", { path: file, root: tmp });
		expect(r.content).toEqual([{ type: "image", data: bytes.toString("base64"), mimeType: "image/png" }]);
	});
	it("read rejects paths outside the root", async () => {
		const r = await readTool.execute("t1", { path: "../escape.txt", root: tmp });
		expect(r.isError).toBe(true);
	});
	it("edit replaces a unique occurrence", async () => {
		const file = path.join(tmp, "a.txt");
		await writeTool.execute("t1", { path: file, content: "hello world\n", root: tmp });
		const r = await editTool.execute("t1", { path: file, oldText: "hello", newText: "goodbye", root: tmp });
		expect(r.isError).toBeFalsy();
		expect(r.details).toMatchObject({ path: file, oldText: "hello", newText: "goodbye" });
		const after = await fs.readFile(file, "utf8");
		expect(after).toBe("goodbye world\n");
	});
	it("edit fails when oldText is not found", async () => {
		const file = path.join(tmp, "b.txt");
		await writeTool.execute("t1", { path: file, content: "x\n", root: tmp });
		const r = await editTool.execute("t1", { path: file, oldText: "missing", newText: "x", root: tmp });
		expect(r.isError).toBe(true);
	});
	it("edit fails when oldText is ambiguous", async () => {
		const file = path.join(tmp, "c.txt");
		await writeTool.execute("t1", { path: file, content: "aaa\n", root: tmp });
		const r = await editTool.execute("t1", { path: file, oldText: "a", newText: "b", root: tmp });
		expect(r.isError).toBe(true);
		expect((r.content[0] as any).text).toContain("3 times");
	});
	it("read with line range returns numbered lines", async () => {
		const file = path.join(tmp, "lines.txt");
		await fs.writeFile(file, "one\ntwo\nthree\nfour\n", "utf8");
		const r = await readTool.execute("t1", { path: file, startLine: 2, endLine: 3, root: tmp });
		const text = (r.content[0] as any).text;
		expect(text).toContain("two");
		expect(text).toContain("three");
		expect(text).not.toContain("one");
		expect(text).not.toContain("four");
	});
	it("multi edit validates every edit before committing", async () => {
		const first = path.join(tmp, "first.txt");
		const second = path.join(tmp, "second.txt");
		await fs.writeFile(first, "alpha", "utf8");
		await fs.writeFile(second, "beta", "utf8");
		const r = await multiEditTool.execute("t1", {
			root: tmp,
			edits: [
				{ path: first, oldText: "alpha", newText: "changed" },
				{ path: second, oldText: "missing", newText: "changed" },
			],
		});
		expect(r.isError).toBe(true);
		expect(await fs.readFile(first, "utf8")).toBe("alpha");
		expect(await fs.readFile(second, "utf8")).toBe("beta");
	});
	it("multi edit commits validated edits together", async () => {
		const first = path.join(tmp, "first.txt");
		const second = path.join(tmp, "second.txt");
		await fs.writeFile(first, "alpha", "utf8");
		await fs.writeFile(second, "beta", "utf8");
		const r = await multiEditTool.execute("t1", {
			root: tmp,
			edits: [
				{ path: first, oldText: "alpha", newText: "one" },
				{ path: second, oldText: "beta", newText: "two" },
			],
		});
		expect(r.isError).toBeFalsy();
		expect(await fs.readFile(first, "utf8")).toBe("one");
		expect(await fs.readFile(second, "utf8")).toBe("two");
	});
});

describe("globTool", () => {
	beforeEach(async () => {
		await fs.writeFile(path.join(tmp, "a.ts"), "");
		await fs.writeFile(path.join(tmp, "b.ts"), "");
		await fs.writeFile(path.join(tmp, "c.txt"), "");
		await fs.mkdir(path.join(tmp, "sub"));
		await fs.writeFile(path.join(tmp, "sub", "d.ts"), "");
	});
	it("matches simple patterns", async () => {
		const r = await globTool.execute("t1", { pattern: "*.ts", root: tmp });
		const text = (r.content[0] as any).text;
		expect(text).toContain("a.ts");
		expect(text).toContain("b.ts");
		expect(text).not.toContain("c.txt");
	});
	it("matches recursive patterns with **", async () => {
		const r = await globTool.execute("t1", { pattern: "**/*.ts", root: tmp });
		const text = (r.content[0] as any).text;
		expect(text).toContain("a.ts");
		expect(text).toContain("sub/d.ts");
	});
	it("returns (no matches) when nothing matches", async () => {
		const r = await globTool.execute("t1", { pattern: "*.py", root: tmp });
		expect((r.content[0] as any).text).toBe("(no matches)");
	});
});

describe("grepTool", () => {
	beforeEach(async () => {
		await fs.writeFile(path.join(tmp, "x.ts"), "alpha\nbeta\ngamma\n");
		await fs.writeFile(path.join(tmp, "y.ts"), "alpha2\ndelta\n");
	});
	it("finds matches in a single file", async () => {
		const r = await grepTool.execute("t1", { pattern: "alpha", path: path.join(tmp, "x.ts"), root: tmp });
		const text = (r.content[0] as any).text;
		expect(text).toContain("alpha");
		expect(text).toContain("x.ts:1");
	});
	it("walks a directory by default", async () => {
		const r = await grepTool.execute("t1", { pattern: "alpha", root: tmp });
		const text = (r.content[0] as any).text;
		expect(text).toContain("alpha");
		expect(text).toContain("alpha2");
	});
	it("honors the include filter", async () => {
		const r = await grepTool.execute("t1", { pattern: "alpha", root: tmp, include: "x.ts" });
		const text = (r.content[0] as any).text;
		expect(text).toContain("x.ts:1");
		expect(text).not.toContain("alpha2");
	});
	it("rejects paths outside the root", async () => {
		const r = await grepTool.execute("t1", { pattern: ".", path: "../escape", root: tmp });
		expect(r.isError).toBe(true);
	});
	it("rejects invalid regex", async () => {
		const r = await grepTool.execute("t1", { pattern: "[unclosed", root: tmp });
		expect(r.isError).toBe(true);
	});
});

describe("builtinTools list", () => {
	it("exports every built-in tool in registration order", () => {
		expect(builtinTools.map((t) => t.name)).toEqual(["bash", "read", "write", "edit", "multi_edit", "glob", "grep"]);
	});
});
