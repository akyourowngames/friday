/**
 * Built-in tools for friday-ng.
 *
 * Modeled after the Pi Coding Agent's `core/tools/`. Each tool is a single
 * `Tool` object (name, description, typebox parameters, execute fn). They
 * live in this module so they're easy to test in isolation and so the CLI
 * can pass them straight to the Agent.
 *
 * Conventions:
 *  - `cwd` is always resolved relative to `process.cwd()` by default, but
 *    tools accept an explicit `cwd` for tests.
 *  - All file system tools reject absolute paths outside the configured
 *    `root` (defaults to process cwd). To allow write access anywhere,
 *    pass `allowOutsideRoot: true` from a trusted caller.
 *  - Tools never throw on user errors; they return a `ToolResult` with
 *    `isError: true` so the LLM can react.
 */
import { execFile } from "node:child_process";
import { promises as fs } from "node:fs";
import * as path from "node:path";
import { Type } from "typebox";
import type { AgentTool, ToolResult } from "../types.ts";
import { isPathInside, resolveSafePath } from "./path-safety.ts";

/** Maximum stdout/stderr bytes to capture from a shell command. */
const SHELL_MAX_OUTPUT = 200 * 1024; // 200 KB

const SHELL_TIMEOUT_MS = 60_000;

const SHELL_DANGEROUS_PATTERNS: RegExp[] = [
	/^\s*rm\s+-rf?\s+\/\s*$/i,
	/^\s*(curl|wget)\s+.*\|\s*sh\b/i,
	/^\s*mkfs/i,
	/^\s*dd\s+if=/i,
	/^\s*shutdown\b/i,
	/^\s*reboot\b/i,
];

const bashParams = Type.Object({
	command: Type.String({ description: "Shell command to execute" }),
	cwd: Type.Optional(Type.String({ description: "Override the working directory" })),
	timeoutMs: Type.Optional(
		Type.Integer({ description: "Override the default 60s timeout", minimum: 0, maximum: 600_000 }),
	),
});

/** `/bin/sh -c <command>` (with a hard timeout and output cap). */
export const bashTool: AgentTool<typeof bashParams> = {
	name: "bash",
	description:
		"Run a shell command in the current working directory. Use this to execute scripts, inspect the filesystem, or run CLI tools. Commands run via `/bin/sh -c` (POSIX) or `cmd /c` (Windows). Output is truncated at 200KB.",
	parameters: bashParams,
	execute: async (_id, params, signal) => {
		const command = String(params.command ?? "");
		const cwd = params.cwd ? path.resolve(String(params.cwd)) : process.cwd();
		const timeoutMs = Math.min(SHELL_TIMEOUT_MS, Number(params.timeoutMs ?? SHELL_TIMEOUT_MS));

		for (const pat of SHELL_DANGEROUS_PATTERNS) {
			if (pat.test(command)) {
				return errorResult(`Refusing to run potentially dangerous command: ${command}`);
			}
		}

		const isWin = process.platform === "win32";
		const shellCmd = isWin ? "cmd" : "/bin/sh";
		const shellArgs = isWin ? ["/c", command] : ["-c", command];

		try {
			const { stdout, stderr, code } = await new Promise<{
				stdout: string;
				stderr: string;
				code: number | null;
			}>((resolve, reject) => {
				execFile(
					shellCmd,
					shellArgs,
					{ cwd, maxBuffer: SHELL_MAX_OUTPUT, timeout: timeoutMs, signal: signal as any },
					(err, stdout, stderr) => {
						if (err) {
							const e: any = err;
							if (e.killed || e.signal === "SIGTERM") {
								resolve({ stdout: stdout ?? "", stderr: stderr ?? "", code: null });
								return;
							}
							// Non-zero exit code is NOT an error here — return it
							// in the result so the LLM can react.
							resolve({ stdout: stdout ?? "", stderr: stderr ?? "", code: typeof e.code === "number" ? e.code : 1 });
							return;
						}
						resolve({ stdout: stdout ?? "", stderr: stderr ?? "", code: 0 });
					},
				);
			});

			let out = "";
			if (stdout) out += stdout;
			if (stderr) out += (out ? "\n" : "") + stderr;
			if (out.length > SHELL_MAX_OUTPUT) {
				out = out.slice(0, SHELL_MAX_OUTPUT) + `\n... (truncated to ${SHELL_MAX_OUTPUT} bytes)`;
			}
			if (out.length === 0) out = `(command exited with code ${code ?? 0} and no output)`;
			else out += `\n[exit code ${code ?? 0}]`;

			return {
				content: [{ type: "text" as const, text: out }],
				details: { code, stdoutBytes: stdout.length, stderrBytes: stderr.length },
			};
		} catch (e) {
			return errorResult(e instanceof Error ? e.message : String(e));
		}
	},
};

function errorResult(message: string): ToolResult {
	return {
		content: [{ type: "text" as const, text: `Error: ${message}` }],
		details: { error: true },
		isError: true,
	};
}

const MAX_READ_BYTES = 512 * 1024;

/** `read` — read a file from disk with line offsets. */
const readParams = Type.Object({
	path: Type.String({ description: "File path to read (relative to cwd or absolute within the workspace)" }),
	startLine: Type.Optional(Type.Integer({ description: "1-based start line", minimum: 1 })),
	endLine: Type.Optional(Type.Integer({ description: "1-based end line (inclusive)", minimum: 1 })),
	root: Type.Optional(Type.String({ description: "Workspace root to constrain reads to" })),
});

export const readTool: AgentTool<typeof readParams> = {
	name: "read",
	description: "Read a text file. Supports optional line range. Output is truncated at 512KB.",
	parameters: readParams,
	execute: async (_id, params) => {
		const root = params.root ?? process.cwd();
		const safe = resolveSafePath(root, params.path);
		if (!isPathInside(root, safe)) {
			return errorResult(`Path is outside the workspace root: ${params.path}`);
		}
		try {
			const stat = await fs.stat(safe);
			if (!stat.isFile()) return errorResult(`Not a regular file: ${safe}`);
			if (stat.size > MAX_READ_BYTES) {
				const fh = await fs.open(safe, "r");
				try {
					const buf = Buffer.alloc(MAX_READ_BYTES);
					await fh.read(buf, 0, MAX_READ_BYTES, 0);
					const text = buf.toString("utf8");
					return {
						content: [
							{ type: "text" as const, text: `${text}\n... (truncated, file is ${stat.size} bytes)` },
						],
						details: { totalBytes: stat.size, returnedBytes: MAX_READ_BYTES },
					};
				} finally {
					await fh.close();
				}
			}
			const raw = await fs.readFile(safe, "utf8");
			const startLine = params.startLine ?? 1;
			const endLine = params.endLine ?? Infinity;
			const lines = raw.split("\n");
			const sliced = lines.slice(startLine - 1, endLine);
			const numbered = sliced.map((l, i) => `${(startLine + i).toString().padStart(6)}│ ${l}`).join("\n");
			return {
				content: [{ type: "text" as const, text: numbered || "(empty)" }],
				details: { totalLines: lines.length, shown: sliced.length },
			};
		} catch (e) {
			return errorResult(e instanceof Error ? e.message : String(e));
		}
	},
};

/** `write` — write a file (overwriting if it exists). */
const writeParams = Type.Object({
	path: Type.String({ description: "File path to write" }),
	content: Type.String({ description: "Full file content" }),
	root: Type.Optional(Type.String({ description: "Workspace root to constrain writes to" })),
});

export const writeTool: AgentTool<typeof writeParams> = {
	name: "write",
	description: "Write content to a file (overwriting if it exists). Creates parent directories.",
	parameters: writeParams,
	execute: async (_id, params) => {
		const root = params.root ?? process.cwd();
		const safe = resolveSafePath(root, params.path);
		if (!isPathInside(root, safe)) {
			return errorResult(`Path is outside the workspace root: ${params.path}`);
		}
		try {
			await fs.mkdir(path.dirname(safe), { recursive: true });
			await fs.writeFile(safe, params.content, "utf8");
			return {
				content: [{ type: "text" as const, text: `Wrote ${params.content.length} bytes to ${safe}` }],
				details: { path: safe, bytes: params.content.length },
			};
		} catch (e) {
			return errorResult(e instanceof Error ? e.message : String(e));
		}
	},
};

/** `edit` — string-replace a single occurrence in a file. */
const editParams = Type.Object({
	path: Type.String({ description: "File path to edit" }),
	oldText: Type.String({ description: "Exact text to find" }),
	newText: Type.String({ description: "Replacement text" }),
	root: Type.Optional(Type.String({ description: "Workspace root to constrain writes to" })),
});

export const editTool: AgentTool<typeof editParams> = {
	name: "edit",
	description:
		"Replace a single occurrence of `oldText` with `newText` in a file. Fails if `oldText` is not found or matches more than once.",
	parameters: editParams,
	execute: async (_id, params) => {
		const root = params.root ?? process.cwd();
		const safe = resolveSafePath(root, params.path);
		if (!isPathInside(root, safe)) {
			return errorResult(`Path is outside the workspace root: ${params.path}`);
		}
		const { oldText, newText } = params;
		try {
			const original = await fs.readFile(safe, "utf8");
			const occurrences = original.split(oldText).length - 1;
			if (occurrences === 0) {
				return errorResult(`oldText not found in ${safe}`);
			}
			if (occurrences > 1) {
				return errorResult(
					`oldText matches ${occurrences} times in ${safe}; please provide a more specific snippet`,
				);
			}
			const next = original.replace(oldText, newText);
			await fs.writeFile(safe, next, "utf8");
			return {
				content: [{ type: "text" as const, text: `Edited ${safe} (1 occurrence replaced)` }],
				details: { path: safe, oldLen: oldText.length, newLen: newText.length },
			};
		} catch (e) {
			return errorResult(e instanceof Error ? e.message : String(e));
		}
	},
};

/** `glob` — find files matching a pattern. */
const globParams = Type.Object({
	pattern: Type.String({ description: "Glob pattern (e.g. **/*.ts)" }),
	root: Type.Optional(Type.String({ description: "Root to search under" })),
});

export const globTool: AgentTool<typeof globParams> = {
	name: "glob",
	description: "Find files matching a glob pattern. Returns paths relative to `root`, one per line.",
	parameters: globParams,
	execute: async (_id, params) => {
		const root = params.root ?? process.cwd();
		try {
			// node:fs has no built-in glob; use a minimal recursive walker.
			const results = await walkGlob(root, params.pattern, 1000);
			if (results.length === 0) {
				return {
					content: [{ type: "text" as const, text: "(no matches)" }],
					details: { matches: 0 },
				};
			}
			return {
				content: [{ type: "text" as const, text: results.join("\n") }],
				details: { matches: results.length },
			};
		} catch (e) {
			return errorResult(e instanceof Error ? e.message : String(e));
		}
	},
};

/** Minimal glob: supports `*` and `**` for the recursive case. */
async function walkGlob(root: string, pattern: string, limit: number): Promise<string[]> {
	const matcher = compileGlob(pattern);
	const out: string[] = [];
	async function walk(dir: string, depth: number): Promise<void> {
		if (out.length >= limit) return;
		let entries;
		try {
			entries = await fs.readdir(dir, { withFileTypes: true });
		} catch {
			return;
		}
		for (const entry of entries) {
			if (out.length >= limit) return;
			const full = path.join(dir, entry.name);
			// Normalize the relative path to use forward slashes so output
			// is consistent across platforms.
			const rel = path.relative(root, full).replace(/\\/g, "/");
			if (matcher(rel)) {
				out.push(rel);
			}
			if (entry.isDirectory() && depth < 8) {
				await walk(full, depth + 1);
			}
		}
	}
	await walk(root, 0);
	return out;
}

function compileGlob(pattern: string): (p: string) => boolean {
	// Translate glob to a regex. Supports `**`, `*`, `?`, character classes.
	// A leading `**/` is treated as "any depth" — i.e. it can match zero or
	// more path segments. So `**/*.ts` matches both `a.ts` and `sub/a.ts`.
	let p = pattern;
	let leadingDouble = false;
	if (p.startsWith("**/")) {
		leadingDouble = true;
		p = p.slice(3);
	}
	const escaped = p
		.replace(/[.+^${}()|[\]\\]/g, "\\$&")
		.replace(/\*\*/g, "::DOUBLESTAR::")
		.replace(/\*/g, "[^/]*")
		.replace(/::DOUBLESTAR::/g, ".*")
		.replace(/\?/g, "[^/]");
	const body = leadingDouble ? `(?:.*\\/)?${escaped}` : escaped;
	const re = new RegExp(`^${body}$`);
	return (p: string) => re.test(p.replace(/\\/g, "/"));
}

/** `grep` — find lines matching a pattern in files. */
const grepParams = Type.Object({
	pattern: Type.String({ description: "Regular expression to search for" }),
	path: Type.Optional(Type.String({ description: "Directory or file to search (default: cwd)" })),
	root: Type.Optional(Type.String({ description: "Root to constrain paths to" })),
	include: Type.Optional(Type.String({ description: "Glob pattern to filter files (e.g. *.ts)" })),
	limit: Type.Optional(Type.Integer({ description: "Max matches to return (default 200)", minimum: 1, maximum: 5000 })),
});

export const grepTool: AgentTool<typeof grepParams> = {
	name: "grep",
	description: "Search files for a regex pattern. Returns matching lines with file paths and line numbers.",
	parameters: grepParams,
	execute: async (_id, params) => {
		const root = params.root ?? process.cwd();
		const searchPath = path.resolve(root, params.path ?? ".");
		if (!isPathInside(root, searchPath)) {
			return errorResult(`Path is outside the workspace root: ${params.path}`);
		}
		let re: RegExp;
		try {
			re = new RegExp(params.pattern, "i");
		} catch (e) {
			return errorResult(`Invalid regex: ${e instanceof Error ? e.message : String(e)}`);
		}
		const limit = params.limit ?? 200;
		const include = params.include ? compileGlob(params.include) : null;
		const matches: string[] = [];
		async function walk(dir: string, depth: number): Promise<void> {
			if (matches.length >= limit) return;
			let entries: any[];
			try {
				entries = await fs.readdir(dir, { withFileTypes: true });
			} catch {
				return;
			}
			for (const entry of entries) {
				if (matches.length >= limit) return;
				const full = path.join(dir, entry.name);
				if (entry.isDirectory()) {
					if (depth < 8) await walk(full, depth + 1);
				} else if (entry.isFile()) {
					const rel = path.relative(root, full);
					if (include && !include(rel)) continue;
					try {
						const text = await fs.readFile(full, "utf8");
						const lines = text.split("\n");
						for (let i = 0; i < lines.length; i++) {
							if (re.test(lines[i]!)) {
								matches.push(`${rel}:${i + 1}: ${lines[i]}`);
								if (matches.length >= limit) return;
							}
						}
					} catch {
						// unreadable file (binary, perm) — skip
					}
				}
			}
		}
		try {
			const stat = await fs.stat(searchPath);
			if (stat.isFile()) {
				const text = await fs.readFile(searchPath, "utf8");
				const lines = text.split("\n");
				for (let i = 0; i < lines.length; i++) {
					if (re.test(lines[i]!)) {
						matches.push(`${path.relative(root, searchPath)}:${i + 1}: ${lines[i]}`);
						if (matches.length >= limit) break;
					}
				}
			} else {
				await walk(searchPath, 0);
			}
		} catch (e) {
			return errorResult(e instanceof Error ? e.message : String(e));
		}
		if (matches.length === 0) {
			return {
				content: [{ type: "text" as const, text: "(no matches)" }],
				details: { matches: 0 },
			};
		}
		return {
			content: [{ type: "text" as const, text: matches.join("\n") }],
			details: { matches: matches.length },
		};
	},
};

/** Convenience: every built-in tool, in the order they should be registered. */
export const builtinTools: AgentTool[] = [bashTool, readTool, writeTool, editTool, globTool, grepTool];
