#!/usr/bin/env -S node --max-old-space-size=4096
/**
 * friday-ng CLI — a streaming AI assistant built on the Pi Agent Harness
 * streaming architecture.
 *
 * Usage:
 *   friday-ng "Hello, what is 2+2?"
 *   friday-ng "Write a haiku" --provider openai
 *   friday-ng "Hello" --provider ollama
 *
 * First run with a real provider: paste your API key, pick a model, and it's saved.
 * Subsequent runs auto-detect and use your saved key + model.
 */
import fs from "node:fs";
import { Agent } from "./agent.ts";
import { ConsoleRenderer } from "./console-renderer.ts";
import { Tui } from "./tui.ts";
import { loadConfig, saveConfig, withLastModel, bumpRecentSession, withLastSessionId } from "./config.ts";
import { setupProvider, listModelsForProvider, buildStreamFunction } from "./interactive.ts";
import { findProvider, listProviders, resolveApiKey, type ProviderMeta } from "./providers/registry.ts";
import { isOllamaRunning } from "./providers/ollama.ts";
import { setupConsoleEncoding, applyWindowsUtf8Default, revertWindowsUtf8Default, readConsoleStatus } from "./console-setup.ts";
import { bashTool, readTool, writeTool, editTool, globTool, grepTool } from "./tools/shell.ts";
import { Type } from "typebox";
import { SettingsStore, listSettings } from "./settings.ts";
import { parseSlashCommand, clearSlashCommands } from "./slash-commands.ts";
import { registerBuiltinCommands } from "./commands/builtin.ts";
import { createSession, loadSession, listSessions, deleteSession, recordMessage, type SessionMeta } from "./sessions.ts";
import { compactTranscript, makeTransformContext } from "./compaction.ts";
import { withRetry } from "./retry.ts";
import { HookRegistry } from "./hooks.ts";
import { buildHost, defaultExtensionsDir, loadExtensions } from "./extension-loader.ts";
import type { AgentMessage, Model } from "./types.ts";

interface CliOptions {
	prompt: string;
	provider?: string;
	model?: string;
	apiKey?: string;
	listProviders: boolean;
	listModels: boolean;
	help: boolean;
	version: boolean;
	noConfig: boolean;
	forceKey: boolean;
	repl: boolean;
	setupUtf8: boolean;
	revertUtf8: boolean;
	utf8Status: boolean;
	/** Resume a specific session by id. */
	resumeSession?: string;
	/** List saved sessions and exit. */
	listSessions: boolean;
	/** Delete a session by id. */
	deleteSession?: string;
}

function parseArgs(argv: string[]): CliOptions {
	const opts: CliOptions = {
		prompt: "",
		listProviders: false,
		listModels: false,
		help: false,
		version: false,
		noConfig: false,
		forceKey: false,
		repl: false,
		setupUtf8: false,
		revertUtf8: false,
		utf8Status: false,
		listSessions: false,
	};
	const positional: string[] = [];

	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--help" || arg === "-h") opts.help = true;
		else if (arg === "--version" || arg === "-v") opts.version = true;
		else if (arg === "--provider") opts.provider = argv[++i];
		else if (arg === "--model") opts.model = argv[++i];
		else if (arg === "--api-key") opts.apiKey = argv[++i];
		else if (arg === "--list-providers") opts.listProviders = true;
		else if (arg === "--list-models") opts.listModels = true;
		else if (arg === "--repl" || arg === "-i") opts.repl = true;
		else if (arg === "--no-config") opts.noConfig = true;
		else if (arg === "--force-key") opts.forceKey = true;
		else if (arg === "--setup-utf8") opts.setupUtf8 = true;
		else if (arg === "--revert-utf8") opts.revertUtf8 = true;
		else if (arg === "--utf8-status") opts.utf8Status = true;
		else if (arg === "--resume") opts.resumeSession = argv[++i];
		else if (arg === "--list-sessions") opts.listSessions = true;
		else if (arg === "--delete-session") opts.deleteSession = argv[++i];
		else if (!arg?.startsWith("--")) positional.push(arg ?? "");
	}

	opts.prompt = positional.join(" ").trim();
	return opts;
}

function getVersion(): string {
	try {
		const pkgPath = new URL("../package.json", import.meta.url);
		const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf-8"));
		return pkg.version ?? "unknown";
	} catch {
		return "unknown";
	}
}

function printHelp(): void {
	const providers = listProviders()
		.map((p) => `  ${p.id.padEnd(14)} ${p.description}`)
		.join("\n");

	console.log(`
  friday-ng  —  AI assistant with instant streaming

  \x1b[1mUSAGE\x1b[0m
    friday-ng                  Start interactive chat (default)
    friday-ng "your question"  Ask a one-shot question
    friday-ng -i               Force interactive mode

  \x1b[1mOPTIONS\x1b[0m
    \x1b[36m--provider <id>\x1b[0m     AI provider (default: saved from last run, or openai)
    \x1b[36m--model <name>\x1b[0m      Skip the model picker, use this model directly
    \x1b[36m--api-key <key>\x1b[0m     Pass API key (won't be saved to config)
    \x1b[36m--no-config\x1b[0m         Don't save key/model to config
    \x1b[36m--force-key\x1b[0m         Re-prompt for API key even if one is saved

  \x1b[1mSESSIONS\x1b[0m
    \x1b[36m--resume <id>\x1b[0m       Resume a saved session
    \x1b[36m--list-sessions\x1b[0m     List saved sessions
    \x1b[36m--delete-session <id>\x1b[0m  Delete a session

  \x1b[1mINFO\x1b[0m
    \x1b[36m--list-providers\x1b[0m    List all providers
    \x1b[36m--list-models\x1b[0m       List models for the selected provider
    \x1b[36m-v, --version\x1b[0m       Show version
    \x1b[36m-h, --help\x1b[0m          Show this help

  \x1b[1mWINDOWS\x1b[0m
    \x1b[36m--setup-utf8\x1b[0m       Make UTF-8 + VT the default (one-time setup)
    \x1b[36m--revert-utf8\x1b[0m      Undo --setup-utf8
    \x1b[36m--utf8-status\x1b[0m      Show current terminal encoding

  \x1b[1mPROVIDERS\x1b[0m
${providers}

  \x1b[1mEXAMPLES\x1b[0m
    friday-ng                             # chat with kilo.ai (default, free models)
    friday-ng "What is 2+2?"              # one-shot question
    friday-ng --provider openai            # use OpenAI
    friday-ng --provider anthropic         # use Claude
    friday-ng --provider ollama            # use local Ollama (no key needed)
    friday-ng --provider google            # use Gemini

  API keys are saved in ~/.friday-ng/config.json (mode 0600).
  kilo.ai has free models (tencent/hy3:free) — just get a key at kilo.ai.`);
}

async function listAvailableModels(providerId: string, apiKey?: string): Promise<void> {
	const provider = findProvider(providerId);
	if (!provider) {
		console.error(`Unknown provider: ${providerId}`);
		process.exit(1);
	}
	const config = await loadConfig();
	const key = apiKey ?? resolveApiKey(providerId) ?? config.providers[providerId]?.apiKey ?? "";
	const baseUrl = config.providers[providerId]?.baseUrl ?? provider.defaultBaseUrl;

	const models = await listModelsForProvider(provider, key, baseUrl);
	if (models.length === 0) {
		console.log(`(Could not fetch model list from ${provider.name})`);
		console.log(`Default: ${provider.defaultModel}`);
	} else {
		for (const m of models) console.log(m);
	}
}

/** Built-in tools for the CLI.
 *
 *  We combine the coding-agent shell tools (bash, read, write, edit, glob, grep)
 *  with two small demo tools (calculator, websearch) so the default experience
 *  is "you can actually do things", not "you can only chat". */
const calculatorTool = {
	name: "calculator",
	description: "Evaluate a simple arithmetic expression and return the result.",
	parameters: Type.Object({
		expression: Type.String({ description: "The arithmetic expression to evaluate" }),
	}),
	execute: async (_id: string, params: any) => {
		try {
			const result = Function(`"use strict"; return (${params.expression})`)() as number;
			return {
				content: [{ type: "text" as const, text: String(result) }],
				details: { result },
			};
		} catch (e) {
			return {
				content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
				details: { error: true },
				isError: true,
			};
		}
	},
};

const websearchTool = {
	name: "websearch",
	description: "Search the web for information.",
	parameters: Type.Object({
		query: Type.String({ description: "Search query" }),
	}),
	execute: async (_id: string, params: any) => {
		return {
			content: [{ type: "text" as const, text: `Search results for: ${params.query}` }],
			details: { simulated: true },
		};
	},
};

/** `codingTools` is the workspace shell toolset, used when the user is in
 *  a real project. `cliTools` is the default set, which also includes the
 *  demo tools (calculator, websearch). */
const codingTools = [bashTool, readTool, writeTool, editTool, globTool, grepTool];
const cliTools = [...codingTools, calculatorTool, websearchTool];

async function main(): Promise<void> {
	// Switch the console to UTF-8 (Windows) and force stdout/stderr to emit
	// UTF-8 bytes. This makes emoji and box-drawing chars render properly on
	// legacy Conhost and any other non-UTF-8 terminal. No-op on POSIX.
	setupConsoleEncoding();

	const opts = parseArgs(process.argv.slice(2));

	// Show the user the UTF-8 state on startup so they know whether emoji
	// will render correctly in the current terminal. We only print this for
	// commands that actually produce visible output (skip --help, --list-*,
	// and the utf8 subcommands themselves to avoid noise).
	const silent =
		opts.help ||
		opts.version ||
		opts.listProviders ||
		opts.listModels ||
		opts.utf8Status ||
		opts.setupUtf8 ||
		opts.revertUtf8;
	if (!silent && process.platform === "win32") {
		const status = readConsoleStatus();
		// Only nag if the VT mode isn't even enabled — that's the most
		// reliable signal that emoji + colors are broken. The codepage
		// reading is unreliable from a Node child process (it reflects the
		// spawn env, not necessarily the user's terminal).
		if (status.vtEnabled === false) {
			console.error(
				"[console] VT processing is disabled — emoji and colors will not render. Run `friday-ng --setup-utf8` to fix.",
			);
		}
	}

	if (opts.help) {
		printHelp();
		return;
	}

	if (opts.version) {
		console.log(`friday-ng ${getVersion()}`);
		return;
	}

	if (opts.listProviders) {
		for (const p of listProviders()) {
			console.log(`${p.id}\t${p.name}\t${p.requiresKey ? "key" : "no-key"}`);
		}
		return;
	}

	if (opts.listModels) {
		const providerId = opts.provider ?? "openai";
		await listAvailableModels(providerId, opts.apiKey);
		return;
	}

	if (opts.listSessions) {
		const sessions = await listSessions();
		if (sessions.length === 0) {
			console.log("(no saved sessions)");
		} else {
			for (const s of sessions) {
				console.log(
					`${s.id}\t${s.updatedAt}\t${s.title}\t${s.provider}/${s.model}\t${s.messageCount} msg`,
				);
			}
		}
		return;
	}

	if (opts.deleteSession) {
		const ok = await deleteSession(opts.deleteSession);
		console.log(ok ? `✓ Deleted ${opts.deleteSession}` : `✗ No such session: ${opts.deleteSession}`);
		return;
	}

	if (opts.utf8Status) {
		const s = readConsoleStatus();
		console.log(
			`Platform:   ${s.platform}\n` +
				`Code page:  ${s.codePage ?? "(unknown)"}${s.codePageIsUtf8 ? " ✓ UTF-8" : ""}\n` +
				`VT mode:    ${s.vtEnabled === null ? "(unknown)" : s.vtEnabled ? "enabled ✓" : "disabled"}`,
		);
	}

	// Load any user-installed extensions under ~/.friday-ng/extensions/.
	// The slash command registry is process-global, so we load it before
	// the help/REPL decision so `/help` shows extension commands too.
	await loadStartupExtensions();

	if (opts.setupUtf8) {
		try {
			applyWindowsUtf8Default();
			console.log("✓ Wrote HKCU\\Console\\CodePage = 65001 (UTF-8)");
			console.log("✓ Wrote HKCU\\Console\\VirtualTerminalLevel = 1 (ANSI escapes)");
			console.log("New cmd.exe / Conhost sessions on this user account will start in UTF-8.");
			console.log("Run `friday-ng --utf8-status` from a fresh terminal to confirm.");
		} catch (err) {
			console.error("✗ Failed:", err instanceof Error ? err.message : String(err));
			process.exit(1);
		}
		return;
	}

	if (opts.revertUtf8) {
		try {
			revertWindowsUtf8Default();
			console.log("✓ Removed HKCU\\Console\\CodePage and VirtualTerminalLevel.");
			console.log("Windows will fall back to the system default codepage (typically 437).");
		} catch (err) {
			console.error("✗ Failed:", err instanceof Error ? err.message : String(err));
			process.exit(1);
		}
		return;
	}

	// No prompt + no --repl → default to interactive mode (the natural thing).
	if (!opts.prompt && !opts.repl) {
		opts.repl = true;
	}

	// Resolve provider: explicit flag → saved config → "openai" → "faux"
	let providerId = opts.provider;
	if (!providerId) {
		const config = await loadConfig();
		providerId = config.lastProvider ?? "kilo";
	}

	// Pre-flight: if Ollama, check if it's running
	if (providerId === "ollama") {
		const running = await isOllamaRunning();
		if (!running) {
			console.error(
				"✗ Ollama is not running. Start it with: ollama serve\n" +
					"  Or use a different provider: --provider openai, --provider anthropic, etc.",
			);
			process.exit(1);
		}
	}

	// Auto-setup: prompt for key if needed, pick model if needed
	const setup = await setupProvider(providerId, {
		modelOverride: opts.model,
		noConfig: opts.noConfig,
		forceKeyPrompt: opts.forceKey,
	});

	// Build the model object for the agent
	const providerMeta = findProvider(providerId)!;
	const model = buildModelObject(providerMeta, setup.model);

	if (opts.repl) {
		await runRepl(opts, setup, providerId, model);
		return;
	}

	const agent = new Agent({
		initialState: {
			systemPrompt: `You are friday-ng, a next-generation AI assistant with instant token streaming. Current model: ${setup.model}. Be helpful, concise, and friendly.`,
			tools: cliTools as any,
			model,
		},
		streamFunction: withRetry(setup.streamFn, {
			enabled: true,
			maxRetries: 3,
			baseDelayMs: 2000,
			onRetry: (e) => console.error(`[retry] attempt ${e.attempt} in ${Math.round(e.delayMs / 1000)}s: ${e.error.message}`),
		}),
		toolExecution: "sequential",
	});

	const renderer = new ConsoleRenderer({ showThinking: false });
	agent.subscribe((event) => renderer.render(event));

	await agent.prompt(opts.prompt);
	await agent.waitForIdle();
}

/** Build the `Model` object the Agent loop needs from provider meta + id. */
function buildModelObject(provider: ProviderMeta, modelId: string): Model {
	return {
		id: modelId,
		name: modelId,
		api: provider.id as any,
		provider: provider.id as any,
		baseUrl: provider.defaultBaseUrl,
		reasoning: false,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		contextWindow: provider.defaultContextWindow,
		maxTokens: provider.defaultMaxTokens,
	};
}

/** Interactive REPL mode: a living Pi-style TUI instead of one-shot. */
async function runRepl(
	opts: CliOptions,
	setup: Awaited<ReturnType<typeof setupProvider>>,
	providerId: string,
	model: Model,
): Promise<void> {
	const providerMeta = findProvider(providerId)!;
	const config = await loadConfig();
	const settings = new SettingsStore({ config });

	// Set up the session: either resume one or start a new one.
	let sessionMeta: SessionMeta;
	let initialMessages: AgentMessage[] = [];
	if (opts.resumeSession) {
		const loaded = await loadSession(opts.resumeSession);
		if (!loaded) {
			console.error(`✗ Session not found: ${opts.resumeSession}`);
			process.exit(1);
		}
		sessionMeta = loaded.meta;
		initialMessages = loaded.messages;
		// Restore the model the user had selected.
		model = buildModelObject(providerMeta, sessionMeta.model);
	} else {
		sessionMeta = await createSession({
			provider: providerId as any,
			model: setup.model,
			apiStyle: providerMeta.apiStyle,
			systemPrompt: `You are friday-ng, a next-generation AI assistant with instant token streaming. Current model: ${setup.model}. Be helpful, concise, and friendly.`,
		});
		// Persist last-session id.
		await saveConfig(withLastSessionId(bumpRecentSession(await loadConfig(), sessionMeta.id), sessionMeta.id));
	}

	const agent = new Agent({
		initialState: {
			systemPrompt: sessionMeta.systemPrompt,
			tools: cliTools as any,
			model,
			messages: initialMessages,
		},
		streamFunction: withRetry(setup.streamFn, {
			enabled: true,
			maxRetries: 3,
			baseDelayMs: 2000,
			onRetry: (e) => console.error(`[retry] attempt ${e.attempt} in ${Math.round(e.delayMs / 1000)}s: ${e.error.message}`),
		}),
		toolExecution: "sequential",
		transformContext: makeTransformContext({
			targetTokens: Math.min(32_000, Math.floor(model.contextWindow * 0.4)),
			preserveTail: 4,
			maxToolResultChars: 3000,
		}),
	});

	// Forward Tui reference to the slash command callbacks (closure-captured).
	const tuiRef: { current: Tui | null } = { current: null };

	// Register built-in slash commands (idempotent). They get a reference
	// to the agent and tui once both are constructed.
	clearSlashCommands();
	const settingsRef = settings;
	registerBuiltinCommands({
		settings: settingsRef,
		listModels: async () => {
			const baseUrl = config.providers[providerId]?.baseUrl ?? providerMeta.defaultBaseUrl;
			const apiKey = config.providers[providerId]?.apiKey ?? "";
			try {
				return await listModelsForProvider(providerMeta, apiKey, baseUrl);
			} catch {
				return [];
			}
		},
		onSelectModel: async (id: string) => {
			await selectModelInRepl(id, agent, tuiRef, providerId, providerMeta, setup);
		},
		onOpenSettings: () => {
			// The /settings command is a stub here — the TUI shows a
			// usage hint. A real UI overlay is left to the host
			// application (a desktop app wrapping friday-ng, for example).
			return undefined;
		},
		onReload: async () => {
			const fresh = await loadConfig();
			settingsRef.replaceConfig(fresh);
		},
		onSwitchProvider: async (id: string) => {
			console.error(`[friday-ng] provider switching mid-session is not yet implemented (asked: ${id})`);
		},
		currentProvider: providerId,
		listProviders: () => listProviders().map((p) => p.id),
		listTools: () => cliTools.map((t) => t.name),
		onCompact: () => undefined,
		listSessions: async () => (await listSessions()).map((s) => s.id),
		onResumeSession: () => undefined,
	});

	const tui = new Tui({
		model: setup.model,
		provider: providerId,
		contextWindow: providerMeta.defaultContextWindow,
		defaultModels: providerMeta.defaultModel
			? [providerMeta.defaultModel]
			: [],
		onSubmit: async (text: string) => {
			await agent.prompt(text);
			await agent.waitForIdle();
		},
		onQuit: () => agent.abort(),
		onListModels: async () => {
			const baseUrl = config.providers[providerId]?.baseUrl ?? providerMeta.defaultBaseUrl;
			const apiKey = config.providers[providerId]?.apiKey ?? "";
			try {
				return await listModelsForProvider(providerMeta, apiKey, baseUrl);
			} catch {
				return [];
			}
		},
		onSelectModel: async (id: string) => {
			await selectModelInRepl(id, agent, tuiRef, providerId, providerMeta, setup);
		},
		onSlashCommand: async (input: string) => {
			const parsed = parseSlashCommand(input);
			if (!parsed) {
				return { handled: false };
			}
			const result = await parsed.command.run({
				tui: tuiRef.current!,
				agent,
				args: parsed.args,
				meta: { provider: providerId, model: setup.model, settings: settingsRef },
			});
			return {
				handled: true,
				message: result.message,
				clearHistory: result.clearHistory,
				quit: result.quit,
			};
		},
	});
	tuiRef.current = tui;

	// Record every message into the session, and forward agent events to the TUI.
	agent.subscribe(async (event) => {
		tui.handleEvent(event);
		if (event.type === "message_end") {
			try {
				await recordMessage(sessionMeta.id, event.message, 0);
			} catch {
				// Best-effort; don't kill the REPL if disk fails.
			}
		}
	});

	await tui.run(opts.prompt);
}

async function selectModelInRepl(
	id: string,
	agent: Agent,
	tuiRef: { current: Tui | null },
	providerId: string,
	providerMeta: ProviderMeta,
	setup: Awaited<ReturnType<typeof setupProvider>>,
): Promise<void> {
	const config = await loadConfig();
	const providerCfg = config.providers[providerId] ?? {};
	const apiKey = providerCfg.apiKey ?? setup.apiKey ?? "";
	const baseUrl = providerCfg.baseUrl ?? setup.baseUrl;
	const authToken = providerCfg.authToken ?? setup.authToken;
	const streamFn = await buildStreamFunction(providerId, {
		model: id,
		apiKey,
		baseUrl,
		authToken,
	});
	agent.useModel(buildModelObject(providerMeta, id), streamFn);
	tuiRef.current?.setModel(id);
	await saveConfig(withLastModel(config, providerId, id));
}

/**
 * Discover and run user-installed extensions from the standard extensions
 * directory. The host exposes a `commands` API, a `hooks` registry, and a
 * settings store. Failures are isolated per extension; the harness keeps
 * running even if one extension throws.
 */
async function loadStartupExtensions(): Promise<void> {
	const dir = defaultExtensionsDir();
	const config = await loadConfig();
	const settings = new SettingsStore({ config });
	const hooks = new HookRegistry();
	const host = buildHost({
		hooks,
		getSetting: (k) => settings.get(k),
		setSetting: (k, v) => settings.set(k, v as any),
		log: (m) => console.error(`[extension] ${m}`),
	});
	const result = await loadExtensions(dir, host);
	if (result.failed.length > 0) {
		for (const f of result.failed) {
			console.error(`[extension] failed to load ${f.file}: ${f.error.message}`);
		}
	}
}

void main().catch((err) => {
	console.error("Fatal error:", err);
	process.exit(1);
});
