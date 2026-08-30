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
import { loadConfig, saveConfig, withLastModel, bumpRecentSession, withLastSessionId, withSettings } from "./config.ts";
import { setupProvider, listModelsForProvider, buildStreamFunction } from "./interactive.ts";
import { findProvider, listProviders, resolveApiKey, type ProviderMeta } from "./providers/registry.ts";
import { isOllamaRunning } from "./providers/ollama.ts";
import { setupConsoleEncoding, applyWindowsUtf8Default, revertWindowsUtf8Default, readConsoleStatus, enableVirtualTerminalProcessing } from "./console-setup.ts";
import { bashTool, readTool, writeTool, editTool, multiEditTool, globTool, grepTool, isDangerousShellCommand } from "./tools/shell.ts";
import { websearchTool } from "./tools/websearch.ts";
import { buildEnvironmentContext } from "./env-context.ts";
import { Type } from "typebox";
import { SettingsStore, settingsToJson } from "./settings.ts";
import { parseSlashCommand, clearSlashCommands } from "./slash-commands.ts";
import { registerBuiltinCommands, type SessionSummary } from "./commands/builtin.ts";
import { createSession, loadSession, listSessions, deleteSession, recordMessage, replaceSessionMessages, updateMeta, type SessionMeta } from "./sessions.ts";
import { compactTranscript, makeTransformContext } from "./compaction.ts";
import { withRetry } from "./retry.ts";
import { HookRegistry } from "./hooks.ts";
import { buildHost, defaultExtensionsDir, loadExtensions } from "./extension-loader.ts";
import { DEFAULT_POLICY, decide } from "./permissions.ts";
import { appendProfile, loadProfile, loadProjectFile, profileDir, profileExists } from "./profile.ts";
import { loadTodos, makeTodoWriteTool, saveTodos, type TodoStore } from "./todos.ts";
import { createCheckpoint, discardCheckpoint, finalizeCheckpoint, listCheckpoints, restoreCheckpoint, type CheckpointManifest } from "./checkpoints.ts";
import type { AgentMessage, Model, Tool } from "./types.ts";

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
 *  with the real websearch tool (DuckDuckGo Instant Answer + Wikipedia
 *  fallback) and a small calculator, so the default experience is "you can
 *  actually do things", not "you can only chat". */
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

/** `codingTools` is the workspace shell toolset, used when the user is in
 *  a real project. `cliTools` is the default set, which also includes the
 *  demo calculator and real web search. */
const codingTools = [bashTool, readTool, writeTool, editTool, multiEditTool, globTool, grepTool, websearchTool];
const baseCliTools = [...codingTools, calculatorTool];

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
	if (!silent && process.platform === "win32" && process.stdout.isTTY) {
		// Nag only when we are on a real terminal that genuinely can't do ANSI.
		// The registry flag alone is a poor signal: it describes *future*
		// consoles, and it is absent (not false) on a stock machine. What
		// matters is whether we managed to switch VT on for this one.
		if (!enableVirtualTerminalProcessing()) {
			console.error(
				"[console] ANSI/VT processing is unavailable — colors, boxes and cursor repaints will not render. " +
					"Run `friday-ng --setup-utf8` to enable it for this and future terminals.",
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
		return;
	}

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
		apiKeyOverride: opts.apiKey,
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

	const oneShotSettings = new SettingsStore({ config: await loadConfig() });
	const maxTokens = Number(oneShotSettings.get("maxTokens"));
	const temperature = Number(oneShotSettings.get("temperature"));
	const agent = new Agent({
		initialState: {
			systemPrompt: await buildSystemPrompt(setup.model),
			tools: baseCliTools as Tool[],
			model,
		},
		streamFunction: withRetry(setup.streamFn, {
			enabled: true,
			maxRetries: 3,
			baseDelayMs: 2000,
			onRetry: (e) => console.error(`[retry] attempt ${e.attempt} in ${Math.round(e.delayMs / 1000)}s: ${e.error.message}`),
		}),
		toolExecution: "sequential",
		maxTokens: maxTokens > 0 ? maxTokens : undefined,
		temperature: temperature >= 0 ? temperature : undefined,
	});

	const renderer = new ConsoleRenderer({ showThinking: oneShotSettings.get("showThinking") === true });
	agent.subscribe((event) => renderer.render(event));

	await agent.prompt(opts.prompt);
	await agent.waitForIdle();
}

/** Build the system prompt: identity + live environment context (OS, shell,
 *  cwd, current date) so the model never needs a tool call to learn them. */
export async function buildSystemPrompt(modelId: string, workspace = process.cwd()): Promise<string> {
	const [profile, project] = await Promise.all([loadProfile(), loadProjectFile(workspace)]);
	return (
		`You are friday-ng, a next-generation AI assistant with instant token streaming. ` +
		`Current model: ${modelId}. Be helpful, concise, and friendly.` +
		(profile ? `\n\n## About the user\n${profile.trim()}\n` : "") +
		(project ? `\n\n## About this project\n${project.trim()}\n` : "") +
		buildEnvironmentContext()
	);
}

/** Build the `Model` object the Agent loop needs from provider meta + id. */
function buildModelObject(provider: ProviderMeta, modelId: string): Model {
	const supportsImages = ["openai", "anthropic", "gemini", "freecc"].includes(provider.apiStyle);
	return {
		id: modelId,
		name: modelId,
		api: provider.id as any,
		provider: provider.id as any,
		baseUrl: provider.defaultBaseUrl,
		reasoning: false,
		input: supportsImages ? ["text", "image"] : ["text"],
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
	const settings = new SettingsStore({ config: await loadConfig() });
	const systemPrompt = await buildSystemPrompt(model.id);
	let sessionMeta: SessionMeta;
	let initialMessages: AgentMessage[] = [];
	if (opts.resumeSession) {
		const loaded = await loadSession(opts.resumeSession);
		if (!loaded) throw new Error(`Session not found: ${opts.resumeSession}`);
		if (loaded.meta.provider !== providerId) {
			throw new Error(`Session ${loaded.meta.id} uses provider ${loaded.meta.provider}; restart with --provider ${loaded.meta.provider}`);
		}
		sessionMeta = loaded.meta;
		initialMessages = loaded.messages;
		model = buildModelObject(providerMeta, sessionMeta.model);
	} else {
		sessionMeta = await createSession({
			provider: providerId as any,
			model: setup.model,
			apiStyle: providerMeta.apiStyle,
			systemPrompt,
		});
		await saveConfig(withLastSessionId(bumpRecentSession(await loadConfig(), sessionMeta.id), sessionMeta.id));
	}
	const sessionRef: { current: SessionMeta } = { current: sessionMeta };
	const tuiRef: { current: Tui | null } = { current: null };
	const pendingCheckpoints = new Map<string, CheckpointManifest>();
	const toolStartedAt = new Map<string, number>();
	const hooks = new HookRegistry();
	let todoStore = await loadTodos(sessionMeta.id);
	const todoTool = makeTodoWriteTool({
		getSessionId: () => sessionRef.current.id,
		onChange: (store) => {
			todoStore = store;
			tuiRef.current?.setTodos(store.items.map((item) => ({ text: item.content, status: item.status })));
		},
	});
	const cliTools: Tool[] = [...baseCliTools, todoTool] as Tool[];
	const maxTokens = Number(settings.get("maxTokens"));
	const temperature = Number(settings.get("temperature"));

	const agent = new Agent({
		sessionId: sessionMeta.id,
		initialState: {
			systemPrompt: await buildSystemPrompt(model.id),
			tools: cliTools,
			model,
			messages: initialMessages,
		},
		streamFunction: withRetry(setup.streamFn, {
			enabled: true,
			maxRetries: 3,
			baseDelayMs: 2000,
			onRetry: (event) => console.error(`[retry] attempt ${event.attempt} in ${Math.round(event.delayMs / 1000)}s: ${event.error.message}`),
		}),
		toolExecution: "sequential",
		maxTokens: maxTokens > 0 ? maxTokens : undefined,
		temperature: temperature >= 0 ? temperature : undefined,
		transformContext: makeTransformContext({
			targetTokens: Number(settings.get("compactAt")) > 0
				? Number(settings.get("compactAt"))
				: Math.min(32_000, Math.floor(model.contextWindow * 0.4)),
			preserveTail: 4,
			maxToolResultChars: 3000,
		}),
		beforeToolCall: async ({ toolCall, args, context }) => {
			const tool = cliTools.find((candidate) => candidate.name === toolCall.name);
			if (!tool) return { block: true, reason: `Unknown tool: ${toolCall.name}` };
			const hookResult = await hooks.trigger("pre_tool_use", { tool, args, callId: toolCall.id });
			if (hookResult.cancel) return { block: true, reason: hookResult.reason ?? "blocked by extension" };
			if (toolCall.name === "bash" && isDangerousShellCommand(String((args as any)?.command ?? ""))) {
				return { block: true, reason: "dangerous shell command denied" };
			}
			const decision = decide(DEFAULT_POLICY, { tool, args });
			if (decision.mode === "deny") return { block: true, reason: decision.reason ?? "denied by policy" };
			if (decision.mode === "ask" && settings.get("confirmToolCalls") === true) {
				const accepted = await tuiRef.current?.confirm(`Run ${toolCall.name}(${JSON.stringify(args)})?`);
				if (!accepted) return { block: true, reason: "declined by user" };
			}
			if (!["write", "edit", "multi_edit", "bash"].includes(toolCall.name)) return undefined;
			const values = (args ?? {}) as Record<string, unknown>;
			const workspace = toolCall.name === "bash"
				? String(values.cwd ?? process.cwd())
				: String(values.root ?? process.cwd());
			const files = toolCall.name === "multi_edit"
				? ((values.edits as Array<{ path?: unknown }> | undefined) ?? []).map((edit) => String(edit.path ?? ""))
				: toolCall.name === "bash" ? undefined : [String(values.path ?? "")];
			try {
				const checkpoint = await createCheckpoint({
					sessionId: sessionRef.current.id,
					workspace,
					files,
					workspaceSnapshot: toolCall.name === "bash",
					exclude: [".friday-ng", ".commandcode", ".cache", ".next", ".turbo"],
					maxFiles: 10_000,
					maxBytes: 100 * 1024 * 1024,
					toolCallId: toolCall.id,
					toolName: toolCall.name,
					todo: todoStore,
					transcript: context.messages.slice(0, -1),
				});
				pendingCheckpoints.set(toolCall.id, checkpoint);
				toolStartedAt.set(toolCall.id, Date.now());
			} catch (error) {
				const accepted = await tuiRef.current?.confirm(`Checkpoint failed: ${error instanceof Error ? error.message : String(error)}. Run without undo?`);
				if (!accepted) return { block: true, reason: "checkpoint failed" };
			}
			return undefined;
		},
		afterToolCall: async ({ toolCall, args, result, isError, context }) => {
			const checkpoint = pendingCheckpoints.get(toolCall.id);
			if (checkpoint) {
				if (isError || result.isError) await discardCheckpoint(checkpoint.sessionId, checkpoint.id);
				else await finalizeCheckpoint(checkpoint.sessionId, checkpoint.id);
				pendingCheckpoints.delete(toolCall.id);
			}
			const tool = cliTools.find((candidate) => candidate.name === toolCall.name);
			if (tool) {
				const hookResult = await hooks.trigger("post_tool_use", {
					tool,
					args,
					callId: toolCall.id,
					result,
					durationMs: Date.now() - (toolStartedAt.get(toolCall.id) ?? Date.now()),
				});
				toolStartedAt.delete(toolCall.id);
				if (hookResult.cancel) return { isError: true, content: [{ type: "text", text: hookResult.reason ?? "blocked by extension" }] };
				return { content: hookResult.result.content, details: hookResult.result.details };
			}
			return undefined;
		},
	});

	const listCurrentModels = async (): Promise<string[]> => {
		const fresh = await loadConfig();
		const baseUrl = fresh.providers[providerId]?.baseUrl ?? setup.baseUrl ?? providerMeta.defaultBaseUrl;
		const apiKey = setup.apiKey ?? fresh.providers[providerId]?.apiKey ?? "";
		try {
			return await listModelsForProvider(providerMeta, apiKey, baseUrl);
		} catch {
			return [];
		}
	};
	const applyRuntimeSettings = (): void => {
		const outputTokens = Number(settings.get("maxTokens"));
		const sampling = Number(settings.get("temperature"));
		agent.maxTokens = outputTokens > 0 ? outputTokens : undefined;
		agent.temperature = sampling >= 0 ? sampling : undefined;
		tuiRef.current?.applySettings({
			showThinking: settings.get("showThinking") === true,
			streamDebounceMs: Number(settings.get("streamDebounceMs")),
		});
	};
	const persistSettings = async (): Promise<void> => {
		const fresh = await loadConfig();
		await saveConfig(withSettings(fresh, settingsToJson(settings)));
		applyRuntimeSettings();
	};
	const refreshPrompt = async (): Promise<void> => {
		const next = await buildSystemPrompt(agent.state.model.id);
		agent.setSystemPrompt(next);
		sessionRef.current = await updateMeta(sessionRef.current.id, { systemPrompt: next });
	};
	const resumeSession = async (id: string): Promise<void> => {
		if (agent.state.isStreaming || agent.hasQueuedMessages()) throw new Error("Wait for the current run before resuming a session.");
		const loaded = await loadSession(id);
		if (!loaded) throw new Error(`Session not found: ${id}`);
		if (loaded.meta.provider !== providerId) throw new Error(`Session uses provider ${loaded.meta.provider}; restart with --provider ${loaded.meta.provider}`);
		const nextStream = await buildStreamFunction(providerId, {
			model: loaded.meta.model,
			apiKey: setup.apiKey ?? "",
			baseUrl: setup.baseUrl,
			authToken: setup.authToken,
		});
		sessionRef.current = loaded.meta;
		todoStore = await loadTodos(loaded.meta.id);
		agent.useSession(loaded.meta.id, loaded.messages);
		agent.useModel(buildModelObject(providerMeta, loaded.meta.model), withRetry(nextStream));
		agent.setSystemPrompt(await buildSystemPrompt(loaded.meta.model));
		tuiRef.current?.setModel(loaded.meta.model);
		tuiRef.current?.loadConversation(loaded.messages);
		tuiRef.current?.setTodos(todoStore.items.map((item) => ({ text: item.content, status: item.status })));
		await saveConfig(withLastSessionId(bumpRecentSession(await loadConfig(), loaded.meta.id), loaded.meta.id));
	};
	const undoLatest = async (): Promise<string> => {
		if (agent.state.isStreaming || agent.hasQueuedMessages()) return "Wait for the current run before undoing.";
		const latest = (await listCheckpoints(sessionRef.current.id)).find((checkpoint) => checkpoint.status === "finalized" && !checkpoint.restoredAt);
		if (!latest) return "No checkpoint to restore.";
		const restored = await restoreCheckpoint({ sessionId: sessionRef.current.id, workspace: latest.workspace, checkpointId: latest.id });
		if (latest.todo && typeof latest.todo === "object") {
			const current = await loadTodos(sessionRef.current.id);
			todoStore = await saveTodos(sessionRef.current.id, latest.todo as TodoStore, current.revision);
		}
		const messages = Array.isArray(latest.transcript) ? latest.transcript as AgentMessage[] : agent.state.messages.slice();
		agent.replaceMessages(messages);
		sessionRef.current = await replaceSessionMessages(sessionRef.current.id, messages);
		tuiRef.current?.loadConversation(messages);
		tuiRef.current?.setTodos(todoStore.items.map((item) => ({ text: item.content, status: item.status })));
		return `Restored checkpoint ${restored.manifest.id}: ${restored.restored.length} restored, ${restored.deleted.length} removed.`;
	};

	clearSlashCommands();
	registerBuiltinCommands({
		settings,
		onSaveSettings: persistSettings,
		listModels: listCurrentModels,
		onSelectModel: async (id) => selectModelInRepl(id, agent, tuiRef, sessionRef, providerId, providerMeta, setup),
		onReload: async () => {
			settings.replaceConfig(await loadConfig());
			applyRuntimeSettings();
			await refreshPrompt();
		},
		onSwitchProvider: async (id) => {
			throw new Error(`Provider switching requires restart: friday-ng --provider ${id}`);
		},
		currentProvider: providerId,
		listProviders: () => listProviders().map((provider) => provider.id),
		listTools: () => cliTools.map((tool) => tool.name),
		onCompact: async () => {
			const result = compactTranscript(agent.state.messages, {
				targetTokens: Number(settings.get("compactAt")) || Math.min(32_000, Math.floor(agent.state.model.contextWindow * 0.4)),
				preserveTail: 4,
				maxToolResultChars: 3000,
			});
			agent.replaceMessages(result.messages);
			sessionRef.current = await replaceSessionMessages(sessionRef.current.id, result.messages);
			tuiRef.current?.loadConversation(result.messages);
		},
		listSessions: async (): Promise<SessionSummary[]> => (await listSessions()).map((entry) => ({
			id: entry.id,
			title: entry.title,
			updatedAt: entry.updatedAt,
			messageCount: entry.messageCount,
		})),
		onResumeSession: resumeSession,
		init: { hasProfile: profileExists, dir: profileDir() },
		profile: { load: loadProfile, append: async (text) => appendProfile(`\n- ${text}\n`), onChanged: refreshPrompt },
		onUndo: undoLatest,
	});
	await loadStartupExtensions(settings, hooks, persistSettings);

	const tui = new Tui({
		model: model.id,
		provider: providerId,
		contextWindow: model.contextWindow,
		defaultModels: providerMeta.defaultModel ? [providerMeta.defaultModel] : [],
		showThinking: settings.get("showThinking") === true,
		streamDebounceMs: Number(settings.get("streamDebounceMs")),
		getSetting: (key) => settings.get(key),
		setSetting: (key, value) => {
			settings.set(key, value as any);
			void persistSettings();
		},
		onSubmit: async (text) => {
			const pre = await hooks.trigger("pre_user_message", { text });
			if (pre.cancel) throw new Error(pre.reason ?? "Message cancelled by extension");
			await agent.prompt(pre.text);
			await agent.waitForIdle();
		},
		onQuit: () => agent.abort(),
		onInterrupt: () => agent.abort(),
		// `/clear` must clear the *agent* and persisted session as well as the
		// painted transcript. Otherwise the next prompt silently carries context
		// the user explicitly asked to discard.
		onClear: () => {
			agent.replaceMessages([]);
			void replaceSessionMessages(sessionRef.current.id, [])
				.then((meta) => { sessionRef.current = meta; })
				.catch((error) => tuiRef.current?.appendSystemLine(`[session] failed to persist clear: ${error instanceof Error ? error.message : String(error)}`));
		},
		onListModels: listCurrentModels,
		onSelectModel: async (id) => selectModelInRepl(id, agent, tuiRef, sessionRef, providerId, providerMeta, setup),
		onSlashCommand: async (input) => {
			const parsed = parseSlashCommand(input);
			if (!parsed) return { handled: false };
			const result = await parsed.command.run({
				tui: tuiRef.current!,
				agent,
				args: parsed.args,
				meta: { provider: providerId, model: agent.state.model.id, settings },
			});
			return { handled: true, ...result };
		},
	});
	tuiRef.current = tui;
	applyRuntimeSettings();
	tui.loadConversation(initialMessages, false);
	tui.setTodos(todoStore.items.map((item) => ({ text: item.content, status: item.status })), false);
	if (!(await profileExists())) tui.appendSystemLine("No profile yet — run /init to personalize friday-ng.", false);

	agent.subscribe(async (event) => {
		tui.handleEvent(event);
		if (event.type === "message_end") {
			const toolCalls = event.message.role === "assistant"
				? event.message.content.filter((content) => content.type === "toolCall").length
				: 0;
			try {
				sessionRef.current = await recordMessage(sessionRef.current.id, event.message, toolCalls);
			} catch {}
			if (event.message.role === "assistant") await hooks.trigger("post_assistant_message", { message: event.message });
		}
	});

	await tui.run(opts.prompt);
}

async function selectModelInRepl(
	id: string,
	agent: Agent,
	tuiRef: { current: Tui | null },
	sessionRef: { current: SessionMeta },
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
	agent.setSystemPrompt(await buildSystemPrompt(id));
	tuiRef.current?.setModel(id);
	sessionRef.current = await updateMeta(sessionRef.current.id, { model: id, systemPrompt: agent.state.systemPrompt });
	await saveConfig(withLastModel(config, providerId, id));
}

/**
 * Discover and run user-installed extensions from the standard extensions
 * directory. The host exposes a `commands` API, a `hooks` registry, and a
 * settings store. Failures are isolated per extension; the harness keeps
 * running even if one extension throws.
 */
async function loadStartupExtensions(
	settings: SettingsStore,
	hooks: HookRegistry,
	onSettingsChanged: () => Promise<void>,
): Promise<void> {
	const dir = defaultExtensionsDir();
	const host = buildHost({
		hooks,
		getSetting: (key) => settings.get(key),
		setSetting: (key, value) => {
			settings.set(key, value as any);
			void onSettingsChanged();
		},
		log: (message) => console.error(`[extension] ${message}`),
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
