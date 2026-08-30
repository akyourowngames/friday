/**
 * Built-in slash commands for friday-ng.
 *
 * Modeled after Pi Coding Agent's slash-commands.ts. Each command is a
 * standalone function `make<Name>Command()` that closes over the deps it
 * needs (the host / agent / settings store) and returns a `SlashCommand`.
 *
 * Commands live in a separate module from the registry so tests can register
 * and unregister them without touching the registry file.
 */
import {
	getSlashCommand,
	listSlashCommands,
	registerSlashCommand,
	type SlashCommand,
	type SlashCommandContext,
	type SlashCommandResult,
} from "../slash-commands.ts";
import type { Agent } from "../agent.ts";
import { getSettingSchema, listSettings, type SettingValue, type SettingsStore } from "../settings.ts";

/** `/help` — list every registered command with its description. */
export function makeHelpCommand(): SlashCommand {
	return {
		name: "/help",
		description: "Show this list of commands.",
		usage: "[command]",
		run: ({ args, tui }: SlashCommandContext): SlashCommandResult => {
			const trimmed = args.trim();
			if (trimmed) {
				const cmd = getSlashCommand(trimmed.startsWith("/") ? trimmed : `/${trimmed}`);
				if (!cmd) {
					return { message: `Unknown command: ${trimmed}` };
				}
				const usage = cmd.usage ? ` ${cmd.usage}` : "";
				const out = `/${cmd.name.replace(/^\//, "")}${usage}\n  ${cmd.description}`;
				return { message: out };
			}
			const all = listSlashCommands();
			const longest = all.reduce((m, c) => Math.max(m, c.name.length), 0);
			const lines = all.map((c) => {
				const usage = c.usage ? ` ${c.usage}` : "";
				return `  ${c.name.padEnd(longest + 2)}${usage.padEnd(0)}\n    ${c.description}`;
			});
			tui.appendSystemLine(`Available commands:\n${lines.join("\n")}`);
			return { message: "(see above)" };
		},
	};
}

/** `/exit`, `/quit` — leave the TUI. */
export function makeExitCommand(): SlashCommand {
	return {
		name: "/exit",
		description: "Quit friday-ng.",
		run: ({ tui }: SlashCommandContext): SlashCommandResult => {
			tui.quitTui();
			return { quit: true };
		},
	};
}

export function makeQuitCommand(): SlashCommand {
	return {
		name: "/quit",
		description: "Alias for /exit.",
		run: ({ tui }: SlashCommandContext): SlashCommandResult => {
			tui.quitTui();
			return { quit: true };
		},
	};
}

/** `/clear` — wipe the in-memory chat history. */
export function makeClearCommand(): SlashCommand {
	return {
		name: "/clear",
		description: "Clear the current chat history.",
		run: ({ tui }: SlashCommandContext): SlashCommandResult => {
			tui.clearHistory();
			return { clearHistory: true };
		},
	};
}

/**
 * `/model` — interactive model picker. The TUI already has a built-in
 * /model selector; this command is a thin wrapper for the case where the
 * slash registry is used outside the TUI's special case.
 */
export function makeModelCommand(deps: { listModels: () => Promise<string[]>; onSelect: (id: string) => Promise<void> | void }): SlashCommand {
	return {
		name: "/model",
		description: "Pick a different model.",
		run: async (): Promise<SlashCommandResult> => {
			const models = await deps.listModels();
			if (models.length === 0) {
				return { message: "No models available." };
			}
			// Defer to the TUI's built-in selector if present, otherwise just
			// pick the first one. The TUI's own /model handler short-circuits
			// before this command runs, so in normal use this path is rarely
			// taken.
			await deps.onSelect(models[0]!);
			return { message: `Switched to ${models[0]!}` };
		},
	};
}

/** `/cost` — print cumulative token + cost stats from the agent. */
export function makeCostCommand(): SlashCommand {
	return {
		name: "/cost",
		description: "Show token usage so far for this session.",
		run: ({ agent, tui }: SlashCommandContext): SlashCommandResult => {
			const totals = computeUsageTotals(agent);
			const msg = formatUsageTotals(totals);
			tui.appendSystemLine(msg);
			return { message: "(see above)" };
		},
	};
}

/** `/usage` — alias for /cost. */
export function makeUsageCommand(): SlashCommand {
	return {
		name: "/usage",
		description: "Alias for /cost.",
		run: ({ agent, tui }: SlashCommandContext): SlashCommandResult => {
			const totals = computeUsageTotals(agent);
			tui.appendSystemLine(formatUsageTotals(totals));
			return { message: "(see above)" };
		},
	};
}

function parseSettingValue(key: string, raw: string): SettingValue {
	const definition = getSettingSchema(key);
	if (!definition) throw new Error(`Unknown setting: ${key}`);
	if (raw === "null") return null;
	if (definition.type === "boolean") {
		if (raw === "true") return true;
		if (raw === "false") return false;
		throw new Error(`Expected true or false for ${key}`);
	}
	if (definition.type === "number") {
		const value = Number(raw);
		if (!Number.isFinite(value)) throw new Error(`Expected a number for ${key}`);
		return value;
	}
	if (definition.type === "stringList") return raw.split(",").map((value) => value.trim()).filter(Boolean);
	return raw;
}

export function makeSettingsCommand(deps: {
	settings: SettingsStore;
	onSave: () => Promise<void> | void;
}): SlashCommand {
	return {
		name: "/settings",
		description: "Show or update user settings.",
		usage: "[key] [value]",
		run: async ({ args }): Promise<SlashCommandResult> => {
			const [key, ...rest] = args.trim().split(/\s+/).filter(Boolean);
			if (!key) {
				return {
					message: listSettings().map((setting) => `${setting.key} = ${JSON.stringify(deps.settings.get(setting.key))}`).join("\n"),
				};
			}
			if (rest.length === 0) return { message: `${key} = ${JSON.stringify(deps.settings.get(key))}` };
			try {
				deps.settings.set(key, parseSettingValue(key, rest.join(" ")));
				await deps.onSave();
				return { message: `${key} = ${JSON.stringify(deps.settings.get(key))}` };
			} catch (error) {
				return { message: `[error] ${error instanceof Error ? error.message : String(error)}` };
			}
		},
	};
}

/** `/reload` — re-read config and re-instantiate the active provider. */
export function makeReloadCommand(deps: { onReload: () => Promise<void> | void }): SlashCommand {
	return {
		name: "/reload",
		description: "Reload the config and re-instantiate the active provider.",
		run: async (): Promise<SlashCommandResult> => {
			await deps.onReload();
			return { message: "Config reloaded." };
		},
	};
}

/** `/provider` — show / switch the active provider. */
export function makeProviderCommand(deps: { onSwitch: (id: string) => Promise<void> | void; currentProvider: string; listProviders: () => string[] }): SlashCommand {
	return {
		name: "/provider",
		description: "Show or switch the active provider.",
		usage: "[provider-id]",
		run: ({ args }): SlashCommandResult | Promise<SlashCommandResult> => {
			const trimmed = args.trim();
			if (!trimmed) {
				const all = deps.listProviders();
				return { message: `Current: ${deps.currentProvider}\nAvailable: ${all.join(", ")}` };
			}
			return Promise.resolve(deps.onSwitch(trimmed)).then(() => ({
				message: `Switched to provider: ${trimmed}`,
			}));
		},
	};
}

/** `/tools` — list the registered tools the agent can call. */
export function makeToolsCommand(deps: { onList: () => string[] }): SlashCommand {
	return {
		name: "/tools",
		description: "List registered tools.",
		run: (): SlashCommandResult => {
			const tools = deps.onList();
			if (tools.length === 0) return { message: "(no tools registered)" };
			return { message: `Tools:\n${tools.map((t) => `  - ${t}`).join("\n")}` };
		},
	};
}

/** `/compact` — ask the host to compact the session. */
export function makeCompactCommand(deps: { onCompact: () => Promise<void> | void }): SlashCommand {
	return {
		name: "/compact",
		description: "Compact the session (summarize old messages).",
		run: async (): Promise<SlashCommandResult> => {
			await deps.onCompact();
			return { message: "Session compacted." };
		},
	};
}

/** A lightweight human-friendly summary of a saved session for `/sessions` /
 *  `/resume`. Hosts feed this from session metadata (title, dates, counts). */
export interface SessionSummary {
	id: string;
	title: string;
	updatedAt: string;
	messageCount: number;
}

function formatRelative(iso: string): string {
	const then = new Date(iso).getTime();
	if (Number.isNaN(then)) return "";
	const secs = Math.floor((Date.now() - then) / 1000);
	if (secs < 60) return "just now";
	if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
	if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
	return `${Math.floor(secs / 86400)}d ago`;
}

/** `/sessions` — list saved sessions with titles + ages instead of a wall of
 *  UUIDs (the previous output in todo.txt made them impossible to navigate). */
export function makeSessionsCommand(deps: { onList: () => Promise<SessionSummary[]> }): SlashCommand {
	return {
		name: "/sessions",
		description: "List saved sessions.",
		run: async (): Promise<SlashCommandResult> => {
			const sessions = await deps.onList();
			if (sessions.length === 0) return { message: "(no saved sessions)" };
			const lines = sessions.map((s, i) => {
				const title = s.title.length > 48 ? s.title.slice(0, 45) + "…" : s.title;
				const age = formatRelative(s.updatedAt);
				return `  ${i + 1}. ${title}${" ".repeat(Math.max(0, 50 - title.length))}${s.messageCount} msgs  ${age}`;
			});
			return { message: `Saved sessions:\n${lines.join("\n")}\n\nRun /resume <index> to open one.` };
		},
	};
}

/** `/resume` — resume a saved session by index or id. */
export function makeResumeCommand(deps: {
	onResume: (id: string) => Promise<void> | void;
	listSessions: () => Promise<SessionSummary[]>;
}): SlashCommand {
	return {
		name: "/resume",
		description: "Resume a saved session.",
		usage: "[index | id]",
		run: async ({ args }): Promise<SlashCommandResult> => {
			const trimmed = args.trim();
			const all = await deps.listSessions();
			if (all.length === 0) return { message: "(no saved sessions)" };
			if (!trimmed) {
				// Show a numbered, navigable list.
				const lines = all.map((s, i) => {
					const title = s.title.length > 40 ? s.title.slice(0, 37) + "…" : s.title;
					return `  ${i + 1}. ${title}  (${formatRelative(s.updatedAt)})`;
				});
				return { message: `Pick one (or /resume <index>):\n${lines.join("\n")}` };
			}
			let id = trimmed;
			const idx = Number.parseInt(trimmed, 10);
			if (Number.isFinite(idx) && String(idx) === trimmed && idx >= 1 && idx <= all.length) {
				id = all[idx - 1]!.id;
			}
			await deps.onResume(id);
			return { message: `Resumed ${all.find((s) => s.id === id)?.title ?? id}` };
		},
	};
}

export function makeInitCommand(deps: { hasProfile: () => Promise<boolean>; dir: string }): SlashCommand {
	return {
		name: "/init",
		description: "Set up or update your personal profile.",
		run: async (): Promise<SlashCommandResult> => {
			const exists = await deps.hasProfile();
			return {
				submitFollowUp: exists
					? `Read PROFILE.md with the read tool using root ${JSON.stringify(deps.dir)}, summarize it, ask what changed, and rewrite it only after I confirm.`
					: `Interview me one question at a time about my name, role, explanation detail, tone, current projects, and anything worth remembering. When complete, write a concise PROFILE.md with the write tool using root ${JSON.stringify(deps.dir)} and path "PROFILE.md".`,
			};
		},
	};
}

export function makeProfileCommand(deps: {
	load: () => Promise<string | undefined>;
	append: (text: string) => Promise<void>;
	onChanged: () => Promise<void> | void;
}): SlashCommand {
	return {
		name: "/profile",
		description: "Show or append to your personal profile.",
		usage: "[edit <text>]",
		run: async ({ args }): Promise<SlashCommandResult> => {
			const trimmed = args.trim();
			if (!trimmed) return { message: (await deps.load()) ?? "No profile yet — run /init." };
			if (!trimmed.startsWith("edit ") || trimmed.slice(5).trim().length === 0) {
				return { message: "Usage: /profile edit <text>" };
			}
			await deps.append(trimmed.slice(5).trim());
			await deps.onChanged();
			return { message: "Profile updated." };
		},
	};
}

export function makeUndoCommand(deps: { onUndo: () => Promise<string> }): SlashCommand {
	return {
		name: "/undo",
		description: "Restore the most recent file checkpoint.",
		run: async (): Promise<SlashCommandResult> => ({ message: await deps.onUndo() }),
	};
}

/** Aggregate token usage from the agent's message log. */
export interface UsageTotals {
	input: number;
	output: number;
	cacheRead: number;
	cacheWrite: number;
	totalTokens: number;
	turns: number;
	toolCalls: number;
}

export function computeUsageTotals(agent: Agent): UsageTotals {
	const totals: UsageTotals = {
		input: 0,
		output: 0,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 0,
		turns: 0,
		toolCalls: 0,
	};
	for (const msg of agent.state.messages) {
		if (msg.role === "assistant") {
			totals.input += msg.usage.input;
			totals.output += msg.usage.output;
			totals.cacheRead += msg.usage.cacheRead;
			totals.cacheWrite += msg.usage.cacheWrite;
			totals.totalTokens += msg.usage.totalTokens;
			totals.turns += 1;
			for (const c of msg.content) {
				if (c.type === "toolCall") totals.toolCalls += 1;
			}
		}
	}
	return totals;
}

export function formatUsageTotals(totals: UsageTotals): string {
	return [
		"Usage:",
		`  Turns:      ${totals.turns}`,
		`  Tool calls: ${totals.toolCalls}`,
		`  Input tok:  ${totals.input.toLocaleString()}`,
		`  Output tok: ${totals.output.toLocaleString()}`,
		`  Cache rd:   ${totals.cacheRead.toLocaleString()}`,
		`  Cache wr:   ${totals.cacheWrite.toLocaleString()}`,
		`  Total tok:  ${totals.totalTokens.toLocaleString()}`,
	].join("\n");
}

/** Register all built-in commands. Idempotent. */
export function registerBuiltinCommands(deps: {
	settings: SettingsStore;
	onSaveSettings: () => Promise<void> | void;
	listModels: () => Promise<string[]>;
	onSelectModel: (id: string) => Promise<void> | void;
	onReload: () => Promise<void> | void;
	onSwitchProvider: (id: string) => Promise<void> | void;
	currentProvider: string;
	listProviders: () => string[];
	listTools: () => string[];
	onCompact: () => Promise<void> | void;
	listSessions: () => Promise<SessionSummary[]>;
	onResumeSession: (id: string) => Promise<void> | void;
	init: { hasProfile: () => Promise<boolean>; dir: string };
	profile: { load: () => Promise<string | undefined>; append: (text: string) => Promise<void>; onChanged: () => Promise<void> | void };
	onUndo: () => Promise<string>;
}): void {
	const tryRegister = (cmd: SlashCommand) => {
		try {
			registerSlashCommand(cmd);
		} catch {
			// already registered — ignore
		}
	};
	tryRegister(makeHelpCommand());
	tryRegister(makeExitCommand());
	tryRegister(makeQuitCommand());
	tryRegister(makeClearCommand());
	tryRegister(makeModelCommand({ listModels: deps.listModels, onSelect: deps.onSelectModel }));
	tryRegister(makeCostCommand());
	tryRegister(makeUsageCommand());
	tryRegister(makeSettingsCommand({ settings: deps.settings, onSave: deps.onSaveSettings }));
	tryRegister(makeReloadCommand({ onReload: deps.onReload }));
	tryRegister(
		makeProviderCommand({
			onSwitch: deps.onSwitchProvider,
			currentProvider: deps.currentProvider,
			listProviders: deps.listProviders,
		}),
	);
	tryRegister(makeToolsCommand({ onList: deps.listTools }));
	tryRegister(makeCompactCommand({ onCompact: deps.onCompact }));
	tryRegister(makeSessionsCommand({ onList: deps.listSessions }));
	tryRegister(makeResumeCommand({ listSessions: deps.listSessions, onResume: deps.onResumeSession }));
	tryRegister(makeInitCommand(deps.init));
	tryRegister(makeProfileCommand(deps.profile));
	tryRegister(makeUndoCommand({ onUndo: deps.onUndo }));
}
