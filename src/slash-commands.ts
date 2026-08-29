/**
 * Slash command registry for friday-ng.
 *
 * Inspired by Pi Coding Agent's `slash-commands.ts`. Each command is a typed
 * `SlashCommand` with a name, description, optional argument schema, and a
 * `run` callback that receives a context object (TUI, agent, etc.).
 *
 * The TUI consults this registry when a message starts with `/`. If the name
 * doesn't match, the message is treated as a normal user prompt (so `/foo`
 * that isn't a command still goes to the LLM — preserves Pi's behavior).
 *
 * Commands are pure: they accept the context and return a result describing
 * what happened. The TUI handles rendering, side-effect state, and turn flow.
 */
import type { Agent } from "./agent.ts";

/** Context passed to a slash command's `run` function. */
export interface SlashCommandContext {
	/** The TUI instance — gives access to history, mode, prompt submission, etc. */
	tui: SlashCommandHost;
	/** The Agent — gives access to state, abort, waitForIdle, prompt, etc. */
	agent: Agent;
	/** The full text the user typed (after the leading `/<name>` and any space). */
	args: string;
	/** Any extra metadata the host wants to pass (provider, model, etc.). */
	meta?: Record<string, unknown>;
}

/** Result returned from a slash command's `run` function. */
export interface SlashCommandResult {
	/**
	 * If set, the TUI should clear the chat history before continuing.
	 * `/clear` uses this.
	 */
	clearHistory?: boolean;
	/**
	 * If set, the TUI should print this string to the chat as a system line
	 * after the command runs (e.g. help output, cost summary, error message).
	 */
	message?: string;
	/**
	 * If true, the TUI should mark the run as "agent needed" — the command
	 * submitted a follow-up prompt that the agent will process. The TUI
	 * will flip to busy mode.
	 */
	submitFollowUp?: string;
	/**
	 * If true, the TUI should quit. `/exit`, `/quit` use this.
	 */
	quit?: boolean;
}

/** Minimal interface the slash command registry needs from a TUI host. */
export interface SlashCommandHost {
	/** Append a system-line entry to the chat history (e.g. help output). */
	appendSystemLine(text: string): void;
	/** Clear the in-memory chat history. */
	clearHistory(): void;
	/** Force a redraw. */
	repaint(): void;
	/** Submit a follow-up user prompt as if the user typed it. */
	submitFollowup(text: string): Promise<void>;
	/** Quit the TUI. */
	quitTui(): void;
	/** Read a setting by key. */
	getSetting(key: string): unknown;
	/** Write a setting by key. */
	setSetting(key: string, value: unknown): void;
}

/** A single registered slash command. */
export interface SlashCommand {
	/** The command name, including the leading slash. E.g. `/help`. */
	name: string;
	/** One-line description shown in `/help` and tab-completion. */
	description: string;
	/**
	 * Optional usage hint appended to the description. E.g.
	 * " — pick a model". The `/help` listing renders this inline.
	 */
	usage?: string;
	/** Run the command. */
	run: (ctx: SlashCommandContext) => Promise<SlashCommandResult> | SlashCommandResult;
}

const commands = new Map<string, SlashCommand>();

/** Register a slash command. Throws if a command with the same name is already registered. */
export function registerSlashCommand(cmd: SlashCommand): void {
	if (commands.has(cmd.name)) {
		throw new Error(`Slash command already registered: ${cmd.name}`);
	}
	commands.set(cmd.name, cmd);
}

/** Look up a registered command by name. */
export function getSlashCommand(name: string): SlashCommand | undefined {
	return commands.get(name);
}

/** List all registered commands (insertion order). */
export function listSlashCommands(): SlashCommand[] {
	return Array.from(commands.values());
}

/**
 * Find commands whose name contains `query` (case-insensitive substring
 * match). Returns up to `limit` results. Used by the TUI's tab-completion
 * / suggestion popup.
 */
export function findCommands(query: string, limit = 10): SlashCommand[] {
	const trimmed = query.trim().toLowerCase();
	if (!trimmed) return listSlashCommands().slice(0, limit);
	const all = listSlashCommands();
	const matches = all.filter((c) => c.name.toLowerCase().includes(trimmed));
	// Sort by best match: name-prefix matches first, then substring.
	matches.sort((a, b) => {
		const aPrefix = a.name.toLowerCase().startsWith(trimmed) ? 0 : 1;
		const bPrefix = b.name.toLowerCase().startsWith(trimmed) ? 0 : 1;
		return aPrefix - bPrefix;
	});
	return matches.slice(0, limit);
}

/** Remove every registered command. Test-only. */
export function clearSlashCommands(): void {
	commands.clear();
}

/**
 * Check if a message is a slash command. Returns the command + remaining
 * args, or undefined if it doesn't look like a command.
 *
 * A message is a slash command if it starts with `/<word>` and the word is
 * a registered command. Otherwise the message is treated as a normal user
 * prompt (so `/foo` that isn't registered goes to the LLM).
 */
export function parseSlashCommand(
	input: string,
): { command: SlashCommand; args: string } | undefined {
	const trimmed = input.trimStart();
	if (!trimmed.startsWith("/")) return undefined;
	// Split on the first whitespace to get the name + the rest.
	const match = /^\/(\S+)(?:\s+([\s\S]*))?$/.exec(trimmed);
	if (!match) return undefined;
	const name = `/${match[1]}`;
	const args = (match[2] ?? "").trim();
	const command = commands.get(name);
	if (!command) return undefined;
	return { command, args };
}
