/**
 * Extension loader for friday-ng.
 *
 * A friday-ng extension is a small JavaScript / TypeScript module that
 * exports a `default` function. The function is called with a `host` object
 * exposing everything an extension might want to register against:
 *   - `addTool(tool)`     register a new AgentTool
 *   - `addCommand(cmd)`   register a new slash command
 *   - `on(event, hook)`   register a lifecycle hook
 *   - `getSetting(key)`   read a setting
 *   - `setSetting(k, v)`  write a setting
 *
 * Extensions are plain ESM modules; they can `import` anything in the
 * project. The loader resolves them relative to a `extensionsDir` (default
 * `~/.friday-ng/extensions/`), discovers every `.js` / `.mjs` / `.ts` file,
 * and calls its `default` export with the host.
 *
 * Failures are isolated: a broken extension should not crash the harness.
 */
import { existsSync, readdirSync, statSync, copyFileSync, mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { join, extname, resolve, dirname } from "node:path";
import { tmpdir } from "node:os";
import { pathToFileURL } from "node:url";
import type { AgentTool } from "./types.ts";
import { HookRegistry } from "./hooks.ts";
import {
	getSlashCommand,
	listSlashCommands,
	registerSlashCommand,
	type SlashCommand,
} from "./slash-commands.ts";

export interface ExtensionHost {
	/** The registry of slash commands. */
	commands: ReturnType<typeof getSlashCommandsApi>;
	/** The registry of hooks. */
	hooks: HookRegistry;
	/** Read-only view of current settings. */
	getSetting(key: string): unknown;
	/** Write a setting. */
	setSetting(key: string, value: unknown): void;
	/** Logger. */
	log(msg: string): void;
}

function getSlashCommandsApi() {
	return {
		register(cmd: SlashCommand): void {
			registerSlashCommand(cmd);
		},
		list(): SlashCommand[] {
			return listSlashCommands();
		},
		get(name: string): SlashCommand | undefined {
			return getSlashCommand(name);
		},
	};
}

/** An extension module. The `default` export is called with the host. */
export type ExtensionModule = {
	default?: (host: ExtensionHost) => void | Promise<void>;
};

/** Result of loading extensions. */
export interface LoadResult {
	loaded: string[];
	failed: { file: string; error: Error }[];
}

/** Discover extension files under `dir`, recursively. */
export function discoverExtensions(dir: string): string[] {
	if (!existsSync(dir)) return [];
	const out: string[] = [];
	for (const entry of readdirSync(dir)) {
		const full = join(dir, entry);
		let st;
		try {
			st = statSync(full);
		} catch {
			continue;
		}
		if (st.isDirectory()) {
			out.push(...discoverExtensions(full));
			continue;
		}
		const ext = extname(full);
		if (ext === ".js" || ext === ".mjs" || ext === ".ts") {
			out.push(full);
		}
	}
	return out;
}

/**
 * Import a single extension file. To work around Node's ESM resolver, which
 * refuses to import `.js` from arbitrary paths (it demands a package.json
 * with `"type": "module"`), we stage the file into a temp directory that
 * has its own package.json, then dynamic-import it.
 *
 * The temp directory is cleaned up after import. For tests and dev, where
 * the file lives under a path Node already considers ESM, we can short-
 * circuit — but the staging path is safe everywhere.
 */
export async function importExtension(file: string): Promise<ExtensionModule> {
	const stage = mkdtempSync(join(tmpdir(), "friday-ext-stage-"));
	try {
		const dest = join(stage, "extension" + extname(file));
		copyFileSync(file, dest);
		writeFileSync(join(stage, "package.json"), JSON.stringify({ type: "module" }));
		const url = pathToFileURL(dest).href;
		return (await import(url)) as ExtensionModule;
	} finally {
		// Best-effort cleanup; ignore failures.
		try {
			rmSync(stage, { recursive: true, force: true });
		} catch {
			// ignored
		}
	}
}

/** Run a single extension module against a host. */
export async function runExtension(mod: ExtensionModule, host: ExtensionHost): Promise<void> {
	if (typeof mod.default === "function") {
		await mod.default(host);
	}
}

/**
 * Load every extension under `dir` against a host. Returns a summary of
 * what was loaded and what failed.
 */
export async function loadExtensions(
	dir: string,
	host: ExtensionHost,
): Promise<LoadResult> {
	const files = discoverExtensions(dir);
	const loaded: string[] = [];
	const failed: { file: string; error: Error }[] = [];
	for (const file of files) {
		try {
			const mod = await importExtension(file);
			await runExtension(mod, host);
			loaded.push(file);
		} catch (err) {
			failed.push({ file, error: err as Error });
		}
	}
	return { loaded, failed };
}

/** Build a host given a hooks registry, a settings store, and a logger. */
export interface BuildHostOptions {
	hooks: HookRegistry;
	getSetting: (k: string) => unknown;
	setSetting: (k: string, v: unknown) => void;
	log?: (msg: string) => void;
}

export function buildHost(opts: BuildHostOptions): ExtensionHost {
	return {
		commands: getSlashCommandsApi(),
		hooks: opts.hooks,
		getSetting: opts.getSetting,
		setSetting: opts.setSetting,
		log: opts.log ?? ((m) => console.error(`[extension] ${m}`)),
	};
}

/** Default extension directory: `~/.friday-ng/extensions/`. */
export function defaultExtensionsDir(homedir = process.env.HOME ?? process.env.USERPROFILE ?? "."): string {
	return join(homedir, ".friday-ng", "extensions");
}

/** Convenience helper for tests. */
export const __testing = { dirname, getSlashCommandsApi, discoverExtensions };
