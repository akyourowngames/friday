/**
 * Pi-style terminal UI for friday-ng.
 *
 * A self-contained, dependency-free TUI that mirrors the parts of the Pi
 * harness (`@earendil-works/pi-tui`) that matter for a chat assistant:
 *
 *   1. Alt-screen mode (the chat owns the whole terminal while running).
 *   2. Differential rendering — we keep the previously drawn frame
 *      (`prevFrame`) and only rewrite the lines that changed, like Pi's
 *      `tui-main-screen.ts` diffing `previousLines` against the new frame.
 *   3. Live, in-place streaming — every `message_update` event repaints the
 *      in-progress assistant message, so tokens appear instantly (no buffering).
 *   4. A REPL: scrollback of past turns + an input line at the bottom.
 *
 * It consumes the exact same `AgentEvent` stream the one-shot
 * `ConsoleRenderer` does, so the "fast token" behavior is identical — the only
 * difference is the surface (a living TUI instead of a single scroll).
 */
import type { AgentEvent, AgentMessage, AssistantMessage } from "./types.ts";
import { markdownSliceByWidth, markdownTruncateToWidth, markdownVisibleWidth, renderMarkdown, renderMarkdownColored } from "./markdown.ts";
import { findCommands } from "./slash-commands.ts";

/** True if the terminal likely supports ANSI colors. */
function detectColorSupport(): boolean {
	if (process.env.NO_COLOR || process.env.FRIDAY_NO_COLOR) return false;
	const so = process.stdout as any;
	const level = process.env.CI ? 0 : (so?.level ?? 0);
	if (level >= 1) return true;
	const term = process.env.TERM;
	if (term && term !== "dumb") return true;
	// Windows: VT is handled by setupConsoleEncoding, so assume yes.
	if (process.platform === "win32") return true;
	return true;
}

const HAS_COLOR = detectColorSupport();

/**
 * Render an assistant's text for the TUI. When colors are supported, uses
 * the colored markdown renderer; otherwise falls back to plain text with
 * indented code blocks.
 */
function renderAssistantText(input: string): string {
	if (HAS_COLOR) {
		return renderMarkdownColored(input).join("\n");
	}
	// Fallback: use the existing plain-text rendering (indented code blocks).
	const lines = renderMarkdown(input);
	const out: string[] = [];
	let inCode = false;
	for (const line of lines) {
		const hasCodeBlock = line.spans.some((s) => s.kind === "code-block");
		if (hasCodeBlock) {
			if (!inCode) {
				out.push("```");
				inCode = true;
			}
			for (const s of line.spans) {
				if (s.kind === "code-block") out.push("  " + s.text);
			}
		} else if (inCode) {
			out.push("```");
			inCode = false;
			out.push(line.spans.map((s) => s.kind === "table" ? s.source.join("\n") : s.text).join(""));
		} else {
			out.push(line.spans.map((s) => s.kind === "table" ? s.source.join("\n") : s.text).join(""));
		}
	}
	if (inCode) out.push("```");
	return out.join("\n");
}

const ESC = "\x1b";
const RESET = `${ESC}[0m`;
const BOLD = `${ESC}[1m`;
const DIM = `${ESC}[2m`;
const CYAN = `${ESC}[36m`;
const GREEN = `${ESC}[32m`;
const MAGENTA = `${ESC}[35m`;
const YELLOW = `${ESC}[33m`;
const RED = `${ESC}[31m`;
const REVERSE = `${ESC}[7m`;

const USER_PREFIX = `${CYAN}you› ${RESET}`;
const USER_PREFIX_VW = 5; // visible width of "you› "
const ASSISTANT_PREFIX = `${MAGENTA}friday› ${RESET}`;
const ASSISTANT_PREFIX_VW = 7;
const TOOL_PREFIX = `${YELLOW}tool ${RESET}`;
const TOOL_PREFIX_VW = 5;

/** Braille spinner frames shown in the status bar while the model is busy. */
const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
/** Block cursor appended to the in-flight assistant message while streaming
 *  (Claude Code style "tokens are arriving" affordance). */
const CURSOR_BLOCK = `${MAGENTA}▍${RESET}`;

export type TuiRole = "user" | "assistant" | "tool" | "system";

export type ToolHistoryStatus = "running" | "done" | "error";

export interface TuiHistoryEntry {
	role: TuiRole;
	text: string;
	toolCallId?: string;
	name?: string;
	args?: unknown;
	status?: ToolHistoryStatus;
	body?: string;
	result?: unknown;
	expanded?: boolean;
}

export interface TuiTodoItem {
	text: string;
	status: string;
}

/** Wrap `text` to `width` columns, prefixing the first line with `prefix` and
 *  continuation lines with spaces of equal visible width. Returns one string
 *  per wrapped line, each terminated with RESET so ANSI state doesn't leak. */
export function wrapText(text: string, width: number, prefix: string, reset: string = RESET): string[] {
	const out: string[] = [];
	const contPrefix = " ".repeat(visibleWidth(prefix));
	const paragraphs = text.split("\n");

	for (const para of paragraphs) {
		if (para.length === 0) {
			out.push(`${prefix}${reset}`);
			continue;
		}
		let line = "";
		let linePrefix = prefix;
		// Split into words and whitespace runs; keep both so spacing is preserved.
		const tokens = para.split(/(\s+)/).filter((t) => t.length > 0);
		for (const tok of tokens) {
			const isSpace = /^\s+$/.test(tok);
			const maxText = Math.max(1, width - visibleWidth(linePrefix));
			if (line.length === 0 && !isSpace && visibleWidth(tok) > maxText) {
				// First token on an empty line but still too long → hard-split
				// at grapheme boundaries so emoji are never sliced mid-codepoint.
				let rem = tok;
				while (visibleWidth(rem) > maxText) {
					const { head } = sliceByWidth(rem, maxText);
					if (head.length === 0) break; // safety: never spin without progress
					out.push(`${linePrefix}${head}${reset}`);
					rem = rem.slice(head.length);
					linePrefix = contPrefix;
				}
				line = rem;
				continue;
			}
			if (visibleWidth(line) + visibleWidth(tok) <= maxText || line.length === 0) {
				line += tok;
			} else if (visibleWidth(tok) <= maxText) {
				out.push(`${linePrefix}${line.trimEnd()}${reset}`);
				line = isSpace ? "" : tok;
				linePrefix = contPrefix;
			} else {
				let rem = tok;
				while (visibleWidth(rem) > maxText) {
					const { head } = sliceByWidth(rem, maxText);
					if (head.length === 0) break; // safety: never spin without progress
					out.push(`${linePrefix}${head}${reset}`);
					rem = rem.slice(head.length);
					linePrefix = contPrefix;
				}
				line = rem;
			}
		}
		if (line.length > 0) out.push(`${linePrefix}${line.trimEnd()}${reset}`);
	}
	return out;
}

/** Split `s` at the first grapheme boundary whose cumulative width would
 *  exceed `max`. Returns the prefix that fits (`head`) and the suffix (`tail`).
 *  Never slices a multi-column grapheme in half. */
function sliceByWidth(s: string, max: number): { head: string; tail: string } {
	const sliced = markdownSliceByWidth(s, max);
	if (sliced.head || !s) return sliced;
	const Segmenter = (globalThis as any).Intl?.Segmenter as
		| (new (locale?: string, options?: { granularity: "grapheme" }) => { segment(value: string): Iterable<{ segment: string }> })
		| undefined;
	const first = Segmenter
		? [...new Segmenter("en", { granularity: "grapheme" }).segment(s)][0]?.segment ?? ""
		: Array.from(s)[0] ?? "";
	return { head: first, tail: s.slice(first.length) };
}

/**
 * Case-insensitive substring filter over a model list. Empty query returns the
 * whole list (so the selector shows everything until the user types).
 */
export function filterModels(all: string[], query: string): string[] {
	const q = query.trim().toLowerCase();
	if (!q) return all;
	return all.filter((m) => m.toLowerCase().includes(q));
}

/**
 * Window a (possibly huge) model list around the cursor so only `rows` fit on
 * screen, marking the cursor row with a reverse-video `▶`. Returns the drawn
 * lines plus the index of the cursor row within them (-1 if empty).
 */
export function visibleSelector(
	list: string[],
	cursor: number,
	rows: number,
	/** Model id to mark with a checkmark (the currently active model). */
	currentModel?: string,
): { lines: string[]; cursorLine: number } {
	const count = list.length;
	if (count === 0) return { lines: [], cursorLine: -1 };
	const start = Math.max(0, Math.min(cursor - Math.floor(rows / 2), Math.max(0, count - rows)));
	const end = Math.min(count, start + Math.max(1, rows));
	const lines: string[] = [];
	let cursorLine = -1;
	for (let i = start; i < end; i++) {
		const isCursor = i === cursor;
		const isCurrent = currentModel ? list[i] === currentModel : false;
		const marker = isCursor ? `${REVERSE}▶ ${RESET}` : "  ";
		const text = (list[i] ?? "").slice(0, Math.max(0, 200));
		const suffix = isCurrent && !isCursor ? ` ${GREEN}✓${RESET}` : "";
		const line = isCursor
			? `${REVERSE}▶ ${text}${RESET}${suffix}`
			: `${marker}${text}${suffix}`;
		lines.push(`${line}${RESET}`);
		if (isCursor) cursorLine = lines.length - 1;
	}
	return { lines, cursorLine };
}

/** Visible (printed) width of a string, ignoring ANSI escape sequences.
 *
 * Walks the string one grapheme cluster at a time (via `Intl.Segmenter`) so
 * emoji, CJK ideographs, and combining marks are measured the way the
 * terminal actually renders them. A bare BMP character is 1 column; an
 * emoji or full-width CJK character is 2 columns; a zero-width joiner or
 * variation selector adds 0.
 *
 * Falls back to a code-unit walk if `Intl.Segmenter` is unavailable (very
 * old Node) — that under-counts emoji but still produces a consistent
 * wrap width.
 */
export function visibleWidth(s: string): number {
	return markdownVisibleWidth(s);
}

/** Width of a single grapheme cluster. 0 for ZWJ/VS sequences, 2 for
 *  emoji + CJK, 1 otherwise. Multi-codepoint clusters (e.g. a family of
 *  three emoji joined with ZWJs) are summed across their constituent
 *  codepoints. */
function graphemeWidth(grapheme: string): number {
	// 0. Pure zero-width / joiner sequences don't take a column.
	if (/^[\u200D\uFE0F\u200B\u200C\u200E\u200F\u2060-\u2064]+$/.test(grapheme)) {
		return 0;
	}
	// For multi-codepoint clusters (ZWJ-joined emoji), sum the widths of
	// the printable codepoints and skip zero-width joiners.
	let width = 0;
	for (const ch of grapheme) {
		const cp = ch.codePointAt(0)!;
		if (
			cp === 0x200d || // ZWJ
			cp === 0xfe0f || // VS-16 (emoji presentation)
			cp === 0x200b || // ZWSP
			cp === 0x200c || // ZWNJ
			cp === 0x200e || // LRM
			cp === 0x200f // RLM
		) {
			continue;
		}
		width += codepointWidth(cp);
	}
	return width;
}

/** Width of a single printable codepoint: 2 for emoji + CJK, 1 otherwise. */
function codepointWidth(cp: number): number {
	if (
		(cp >= 0x1100 && cp <= 0x115f) || // Hangul Jamo
		(cp >= 0x2e80 && cp <= 0x303e) || // CJK Radicals
		(cp >= 0x3041 && cp <= 0x33ff) || // Hiragana/Katakana/CJK
		(cp >= 0x3400 && cp <= 0x4dbf) || // CJK Extension A
		(cp >= 0x4e00 && cp <= 0x9fff) || // CJK Unified
		(cp >= 0xa000 && cp <= 0xa4cf) || // Yi
		(cp >= 0xac00 && cp <= 0xd7a3) || // Hangul Syllables
		(cp >= 0xf900 && cp <= 0xfaff) || // CJK Compatibility
		(cp >= 0xfe30 && cp <= 0xfe4f) || // CJK Compatibility Forms
		(cp >= 0xff00 && cp <= 0xff60) || // Fullwidth Forms
		(cp >= 0xffe0 && cp <= 0xffe6) || // Fullwidth Signs
		(cp >= 0x1f300 && cp <= 0x1f64f) || // Misc Symbols & Pictographs + Emoticons
		(cp >= 0x1f900 && cp <= 0x1f9ff) || // Supplemental Symbols
		(cp >= 0x1fa70 && cp <= 0x1faff) // Symbols & Pictographs Extended-A
	) {
		return 2;
	}
	return 1;
}
function resultText(result: unknown): string {
	const r = (result ?? {}) as { content?: { type?: string; text?: string }[] };
	return (r.content ?? []).filter((c) => c?.type === "text").map((c) => c.text ?? "").join("\n").trim();
}

function diffDetailLines(name: string, result: unknown): string[] {
	const details = ((result ?? {}) as { details?: any }).details;
	if (!details || !["edit", "write", "multiEdit", "multiedit"].includes(name)) return [];
	const edits = name.toLowerCase() === "multiedit"
		? (Array.isArray(details) ? details : details.edits ?? details.changes ?? [])
		: [details];
	const out: string[] = [];
	for (const edit of edits) {
		if (!edit || typeof edit !== "object") continue;
		if (typeof edit.path === "string") out.push(`${DIM}${edit.path}${RESET}`);
		if (typeof edit.oldText === "string") {
			for (const line of edit.oldText.split("\n")) out.push(`${RED}- ${line}${RESET}`);
		}
		if (typeof edit.newText === "string") {
			for (const line of edit.newText.split("\n")) out.push(`${GREEN}+ ${line}${RESET}`);
		}
	}
	return out;
}

export function renderToolEntry(entry: TuiHistoryEntry, width: number): string[] {
	if (!entry.name || !entry.status) return wrapText(entry.text, width, TOOL_PREFIX);
	const color = entry.status === "error" ? RED : entry.status === "done" ? GREEN : YELLOW;
	const icon = entry.status === "error" ? "✗" : entry.status === "done" ? "✓" : "●";
	const title = `${icon} ${formatToolCall(entry.name, entry.args)}`;
	const summary = entry.status === "running" ? "running" : summarizeToolResult(entry.name, entry.result, entry.status === "error");
	let bodyLines = entry.body?.split("\n") ?? [];
	if (bodyLines.length === 0 && entry.expanded) bodyLines = resultText(entry.result).split("\n").filter(Boolean);
	const diffs = diffDetailLines(entry.name, entry.result);
	if (diffs.length > 0) bodyLines = diffs;
	const fullCount = bodyLines.length;
	if (!entry.expanded && bodyLines.length > 12) bodyLines = bodyLines.slice(0, 12);
	const body = [`${DIM}${summary}${RESET}`, ...bodyLines];
	if (!entry.expanded && fullCount > 12) body.push(`${DIM}… ${fullCount - 12} more lines (Ctrl+O)${RESET}`);
	const innerWidth = Math.max(1, width - 4);
	const wrapped = body.flatMap((line) => wrapText(line, innerWidth, ""));
	const contentW = Math.max(visibleWidth(title), ...wrapped.map(visibleWidth), 1);
	const inner = Math.max(1, Math.min(innerWidth, contentW));
	const topTitle = truncateToWidth(title, inner);
	const topPad = Math.max(0, inner - visibleWidth(topTitle));
	const lines = [`${color}╭${RESET} ${topTitle}${" ".repeat(topPad)} ${color}╮${RESET}`];
	for (const line of wrapped) {
		const clipped = truncateToWidth(line, inner);
		lines.push(`${color}│${RESET} ${clipped}${" ".repeat(Math.max(0, inner - visibleWidth(clipped)))} ${color}│${RESET}`);
	}
	lines.push(`${color}╰${"─".repeat(inner + 2)}╯${RESET}`);
	return lines;
}

export function renderTodos(todos: readonly TuiTodoItem[], width: number, maxLines: number): string[] {
	if (maxLines <= 0 || todos.length === 0) return [];
	const marker = (status: string) => status === "completed" ? `${GREEN}✓${RESET}` : status === "in_progress" ? `${YELLOW}●${RESET}` : `${DIM}○${RESET}`;
	const lines = todos.slice(0, maxLines).map((todo) => truncateToWidth(`${marker(todo.status)} ${todo.text}`, width));
	if (todos.length > maxLines) lines[maxLines - 1] = truncateToWidth(`${DIM}… ${todos.length - maxLines + 1} more todos${RESET}`, width);
	return lines;
}

/** Render one history entry to lines. User/assistant messages are enclosed in
 *  labeled rounded boxes (Claude Code / Codex style) so every response is a
 *  self-contained block that can never bleed into the status bar; tool and
 *  system entries stay as compact dim lines. Shared by the pure
 *  `computeContentLines` helper and the Tui's per-entry render cache. */
export function renderHistoryEntry(entry: TuiHistoryEntry, width: number): string[] {
	switch (entry.role) {
		case "user":
			return renderBox(entry.text, width, "you", CYAN);
		case "assistant":
			return renderBox(entry.text, width, "friday", MAGENTA);
		case "tool":
			return renderToolEntry(entry, width);
		case "system":
			return wrapText(entry.text, width, DIM);
		default:
			return [];
	}
}

/** Render `text` as a rounded, labeled box. The box shrinks to fit its
 *  content (up to `width`), every line is padded to the same visible width,
 *  and — critically — the box is never wider than `width`, which the callers
 *  keep one column short of the terminal so nothing can ever wrap or scroll
 *  the screen. `open` omits the bottom border (used while a reply streams). */
function renderBox(text: string, width: number, label: string, color: string, open = false): string[] {
	const maxInner = Math.max(10, width - 4); // │ + space + content + space + │
	const wrapped = text.length > 0 ? wrapText(text, maxInner, "") : [""];
	const contentW = Math.max(0, ...wrapped.map((l) => visibleWidth(l)));
	const inner = Math.max(12, Math.min(maxInner, Math.max(contentW, label.length + 4)));
	const lines: string[] = [];
	if (inner >= label.length + 4) {
		const dash = Math.max(0, inner - label.length - 1);
		lines.push(`${color}╭─ ${BOLD}${label}${RESET} ${"─".repeat(dash)}${color}╮${RESET}`);
	} else {
		lines.push(`${color}╭${"─".repeat(inner + 2)}╮${RESET}`);
	}
	for (const line of wrapped) {
		const pad = Math.max(0, inner - visibleWidth(line));
		lines.push(`${color}│${RESET} ${line}${" ".repeat(pad)} ${color}│${RESET}`);
	}
	if (!open) {
		lines.push(`${color}╰${"─".repeat(inner + 2)}╯${RESET}`);
	}
	return lines;
}

/** Compute the scrollback + streaming content lines (everything above the
 *  status/input reserved rows). Pure and testable. */
export function computeContentLines(
	history: TuiHistoryEntry[],
	streamingText: string,
	width: number,
): string[] {
	const lines: string[] = [];
	for (const entry of history) {
		lines.push(...renderHistoryEntry(entry, width));
	}
	if (streamingText.length > 0) {
		// The in-flight reply is an open-bottomed box (matches the TUI render).
		lines.push(...renderBox(`${streamingText}${CURSOR_BLOCK}`, width, "friday", MAGENTA, true));
	}
	return lines;
}

/** Truncate a string (which may contain ANSI escapes) to `max` visible
 *  columns. Escape sequences have zero width and are never cut mid-sequence;
 *  if any SGR escape was emitted, RESET is appended so state doesn't leak.
 *  This is the last line of defense against lines wrapping over the frame —
 *  the bug that made the status bar collide with the input prompt. */
export function truncateToWidth(s: string, max: number): string {
	return markdownTruncateToWidth(s, max);
}

/** Return the indices of `next` whose line differs from `prev`, plus the
 *  indices that exist in `prev` but not in `next` (lines to clear). */
export function diffFrame(prev: string[] | null, next: string[]): { changed: number[]; cleared: number[] } {
	const changed: number[] = [];
	const cleared: number[] = [];
	const len = Math.max(prev?.length ?? 0, next.length);
	for (let i = 0; i < len; i++) {
		const p = prev?.[i];
		const n = next[i];
		if (p === undefined && n === undefined) continue;
		if (p === undefined) changed.push(i);
		else if (n === undefined) cleared.push(i);
		else if (p !== n) changed.push(i);
	}
	return { changed, cleared };
}

/** Build the welcome banner: a Claude Code style box whose width always fits
 *  the terminal. Pure and testable — every box line has exactly the same
 *  visible width (the old hard-coded padding misaligned the right border, and
 *  on narrow terminals the border got hard-split into garbage). Falls back to
 *  plain lines when the terminal is too narrow for a box. */
export function buildWelcomeBox(cols: number, provider: string, model: string): string[] {
	const contentLines = [
		`${BOLD}${GREEN}friday-ng${RESET}`,
		``,
		`${DIM}Model: ${provider}/${model}${RESET}`,
		``,
		`${DIM}Type a message to start chatting${RESET}`,
		`${DIM}/help for commands • Ctrl+C to quit${RESET}`,
	];
	const widest = Math.max(...contentLines.map((l) => visibleWidth(l)));
	const maxBox = Math.max(0, cols - 2);
	const desired = widest + 4; // 2 spaces of padding on each side
	const boxWidth = Math.min(Math.max(desired, 46), 54, maxBox);
	if (boxWidth < desired || maxBox < desired) {
		// Terminal too narrow for a box — degrade gracefully to plain lines.
		return contentLines;
	}
	const inner = boxWidth - 2;
	const padInner = (s: string) => {
		const pad = Math.max(0, inner - visibleWidth(s) - 2);
		return `${YELLOW}│${RESET}  ${s}${" ".repeat(pad)}${YELLOW}│${RESET}`;
	};
	const lines: string[] = [`${YELLOW}┌${"─".repeat(boxWidth - 2)}┐${RESET}`];
	lines.push(padInner(""));
	for (const line of contentLines) lines.push(padInner(line));
	lines.push(padInner(""));
	lines.push(`${YELLOW}└${"─".repeat(boxWidth - 2)}┘${RESET}`);
	return lines;
}

export interface TuiOptions {
	out?: (text: string) => void;
	input?: NodeJS.ReadStream;
	getSize?: () => [number, number];
	model: string;
	provider: string;
	/** Default/fallback model list to show if the network call fails. */
	defaultModels?: string[];
	onSubmit: (text: string) => Promise<void> | void;
	onQuit?: () => void;
	showThinking?: boolean;
	/** Context window size (tokens) for the active model — used for usage %. */
	contextWindow?: number;
	/** Returns the live model list (CLI wires this to the gateway). */
	onListModels?: () => Promise<string[]>;
	/** Called when the user picks a model in the `/model` selector. */
	onSelectModel?: (id: string) => Promise<void> | void;
	/** Called by the `/clear` command (optional; history is always wiped). */
	onClear?: () => void;
	/** Called when the user presses Ctrl+C while the agent is busy. Hosts
	 *  should abort the running operation (like Claude Code's interrupt). */
	onInterrupt?: () => void;
	/** Optional: parse a slash command. If it returns a result, the TUI
	 *  applies the result instead of submitting to the LLM. The default
	 *  is to only handle the built-in /model and /clear (preserving
	 *  current behavior). */
	onSlashCommand?: (
		input: string,
	) => Promise<{ handled: boolean; message?: string; clearHistory?: boolean; quit?: boolean; submitFollowUp?: string }> | { handled: boolean; message?: string; clearHistory?: boolean; quit?: boolean; submitFollowUp?: string };
	streamDebounceMs?: number;
	getSetting?: (key: string) => unknown;
	setSetting?: (key: string, value: unknown) => void;
}

export class Tui {
	private write: (text: string) => void;
	private input: NodeJS.ReadStream;
	private getSize: () => [number, number];
	private model: string;
	private provider: string;
	private onSubmit: (text: string) => Promise<void> | void;
	private onQuit?: () => void;
	private onListModels?: () => Promise<string[]>;
	private onSelectModel?: (id: string) => Promise<void> | void;
	private onClear?: () => void;
	private onInterrupt?: () => void;
	private defaultModels: string[] = [];
	private onSlashCommand?: TuiOptions["onSlashCommand"];
	private showThinking: boolean;
	private contextWindow: number;
	private streamDebounceMs: number;
	private getSettingHook?: (key: string) => unknown;
	private setSettingHook?: (key: string, value: unknown) => void;

	private history: TuiHistoryEntry[] = [];
	private todos: TuiTodoItem[] = [];
	private confirmationQueue: { prompt: string; resolve: (value: boolean) => void }[] = [];
	private activeConfirmation?: { prompt: string; resolve: (value: boolean) => void };
	private streamingText = "";
	private inputBuffer = "";
	private status = "";
	private busy = false;
	/** Elapsed time tracking: set when busy starts, cleared when it ends. */
	private busyStartTime = 0;
	private elapsedTimer: ReturnType<typeof setInterval> | null = null;
	/** Cached token count from the last agent event. The TUI shows this in the status line. */
	private lastUsage = { input: 0, output: 0, total: 0 };
	/** In-memory input history (most recent last). The user can press ↑/↓ to
	 *  navigate previous submissions. */
	private inputHistory: string[] = [];
	/** Cursor position inside `inputHistory` while browsing. -1 = not browsing. */
	private inputHistoryCursor = -1;
	/** The input that was in the buffer when the user first pressed ↑. We
	 *  restore it when the user walks past the end of the history. */
	private inputHistoryDraft = "";
	/** Cursor offset into the editable `inputBuffer` (UTF-16 index). Readline
	 *  editing keys (←/→, Ctrl+A/E/U/K/W, Home/End) move and edit here. */
	private cursorPos = 0;
	/** Reverse-history search state (Ctrl+R). */
	private searchActive = false;
	private searchQuery = "";
	private searchMatches: string[] = []; // most-recent-first matches
	private searchCursor = -1; // index into searchMatches
	/** The input draft to restore when search is cancelled. */
	private searchDraft = "";
	/** Paste-detection timer: when Enter is pressed, we wait a few ms
	 *  to see if more input is coming (paste). If so, we buffer it
	 *  instead of submitting immediately. */
	private pasteTimer: ReturnType<typeof setTimeout> | null = null;
	private pasteBuffer = "";

	/** "chat" = normal input; "selector" = the `/model` picker overlay. */
	private mode: "chat" | "selector" = "chat";
	private selectorAll: string[] = [];
	private selectorFiltered: string[] = [];
	private selectorFilter = "";
	private selectorCursor = 0;
	private selectorLoading = false;

	/** User scroll offset (negative = scrolled up into history). */
	private userScroll = 0;
	private prevFrame: string[] | null = null;

	/** Per-entry cache of rendered (wrapped) lines so per-token repaints don't
	 *  re-render markdown + re-wrap the whole transcript every time. */
	private entryCache = new WeakMap<TuiHistoryEntry, string[]>();
	private cacheWidth = -1;
	/** Coalesced-render timer (tokens can arrive far faster than we paint). */
	private renderTimer: ReturnType<typeof setTimeout> | null = null;
	private pendingCoalesce = false;

	/** Current slash-command suggestions shown in the popup, if any. */
	private suggestions: { name: string; description: string }[] = [];
	/** Whether the suggestion popup is open. */
	private showSuggestions = false;
	/** Highlighted row in the suggestion popup (↑/↓ navigates it). */
	private suggestionCursor = 0;

	private resolveRun?: () => void;
	private onData = (chunk: Buffer) => this.handleInput(chunk);
	/** Terminal resized → drop the cached frame and repaint from scratch. */
	private onResize = (): void => {
		this.prevFrame = null;
		this.render();
	};

	constructor(options: TuiOptions) {
		this.write = options.out ?? ((text) => process.stdout.write(text));
		this.input = options.input ?? process.stdin;
		this.getSize = options.getSize ?? (() => [process.stdout.columns ?? 80, process.stdout.rows ?? 24]);
		this.model = options.model;
		this.provider = options.provider;
		this.onSubmit = options.onSubmit;
		this.onQuit = options.onQuit;
		this.onListModels = options.onListModels;
		this.onSelectModel = options.onSelectModel;
		this.onClear = options.onClear;
		this.onInterrupt = options.onInterrupt;
		this.defaultModels = options.defaultModels ?? [];
		this.onSlashCommand = options.onSlashCommand;
		this.showThinking = options.showThinking ?? false;
		this.contextWindow = options.contextWindow ?? 0;
		this.streamDebounceMs = Math.max(0, options.streamDebounceMs ?? 32);
		this.getSettingHook = options.getSetting;
		this.setSettingHook = options.setSetting;
	}

	/** Enter the TUI. Resolves when the user quits. `initialPrompt`, if given,
	 *  is submitted as the first message once the screen is up. */
	async run(initialPrompt?: string): Promise<void> {
		this.write(`${ESC}[?1049h`); // enter alt screen
		this.write(`${ESC}[?25l`); // hide cursor while drawing

		// Welcome banner — Claude Code style box. Width-aware: the box always
		// fits the current terminal (the old hard-coded padding misaligned the
		// right border, and on narrow terminals the border got hard-split into
		// garbage like the captured session in todo.txt).
		const [cols] = this.getSize();
		const welcome = buildWelcomeBox(cols, this.provider, this.model).map((text) => ({ role: "system" as const, text }));
		this.history = [...welcome, ...this.history];
		this.render();

		try {
			this.input.setRawMode?.(true);
		} catch {
			// Non-TTY stdin (piped input, some CI pty hosts): keep going without
			// raw mode instead of crashing the whole CLI on startup.
		}
		this.input.resume();
		// Force the input stream to emit UTF-8 strings so emoji typed at the
		// keyboard survive the round-trip into the TUI. On Windows the
		// console's input codepage is normally 437; we set it to 65001 in
		// setupConsoleEncoding() before this point, so the raw bytes we
		// receive are valid UTF-8. This `setEncoding` is belt-and-suspenders
		// in case any byte sequence happens to slip through.
		if (typeof (this.input as any).setEncoding === "function") {
			(this.input as any).setEncoding("utf8");
		}
		this.input.on("data", this.onData);

		// Repaint from a clean frame when the terminal is resized (cols/rows
		// change mid-session; stale frame geometry would otherwise garble the
		// screen until the next full redraw).
		(process.stdout as any)?.on?.("resize", this.onResize);

		const promise = new Promise<void>((resolve) => {
			this.resolveRun = resolve;
		});

		if (initialPrompt && initialPrompt.trim()) {
			this.inputBuffer = initialPrompt;
			void this.submit();
		}

		await promise;
	}

	/** Feed an agent event into the TUI (same protocol as ConsoleRenderer). */
	handleEvent(event: AgentEvent): void {
		switch (event.type) {
			case "message_start":
				if (event.message.role === "user") {
					this.appendHistory({ role: "user", text: this.userText(event.message) });
					this.userScroll = 0;
				} else if (event.message.role === "assistant") {
					this.streamingText = "";
					this.userScroll = 0;
				}
				break;

			case "message_update":
				if (event.message.role === "assistant") {
					this.streamingText = this.assistantText(event.message);
					const usage = (event.message as any).usage;
					if (usage) {
						this.lastUsage = {
							input: usage.input ?? 0,
							output: usage.output ?? 0,
							total: usage.totalTokens ?? 0,
						};
					}
					this.userScroll = 0;
					// Don't repaint per token — coalesce to one frame per ~32ms.
					this.pendingCoalesce = true;
				}
				break;

			case "message_end":
				if (event.message.role === "assistant") {
					const m = event.message;
					const text = this.assistantText(m);
					const hasToolCalls = m.content.some((c) => c.type === "toolCall");
					// Tool-only replies render as the tool execution lines
					// below — don't add a silent empty box for them.
					if (text.trim().length > 0) {
						this.appendHistory({ role: "assistant", text });
					} else if (m.errorMessage) {
						this.appendHistory({ role: "system", text: `[error] ${m.errorMessage}` });
					} else if (!hasToolCalls) {
						this.appendHistory({ role: "system", text: "(empty response)" });
					}
					this.streamingText = "";
					const usage = (event.message as any).usage;
					if (usage) {
						this.lastUsage = {
							input: usage.input ?? 0,
							output: usage.output ?? 0,
							total: usage.totalTokens ?? 0,
						};
					}
				}
				break;

			case "tool_execution_start":
				this.appendHistory({
					role: "tool",
					text: formatToolCall(event.toolName, event.args),
					toolCallId: event.toolCallId,
					name: event.toolName,
					args: event.args,
					status: "running",
					body: "",
				});
				break;

			case "tool_execution_progress": {
				const entry = this.findToolEntry(event.toolCallId);
				if (entry) {
					const chunk = event.progress.content
						.filter((content) => content.type === "text")
						.map((content) => content.text)
						.join("");
					entry.body = `${entry.body ?? ""}${chunk}`;
					this.entryCache.delete(entry);
				}
				this.pendingCoalesce = true;
				break;
			}

			case "tool_execution_end": {
				const entry = this.findToolEntry(event.toolCallId);
				if (entry) {
					entry.status = event.isError ? "error" : "done";
					entry.result = event.result;
					entry.name = event.toolName;
					const summary = summarizeToolResult(event.toolName, event.result, event.isError).split("\n").join("\n  ");
					entry.text = `${event.isError ? "✗" : "✓"} ${formatToolCall(event.toolName, entry.args)}${summary ? ` · ${summary}` : ""}`;
					this.entryCache.delete(entry);
				} else {
					const summary = summarizeToolResult(event.toolName, event.result, event.isError).split("\n").join("\n  ");
					this.appendHistory({ role: "tool", text: `${event.isError ? "✗" : "✓"} ${formatToolCall(event.toolName, {})}${summary ? ` · ${summary}` : ""}`, toolCallId: event.toolCallId, name: event.toolName, args: {}, status: event.isError ? "error" : "done", result: event.result });
				}
				break;
			}
		}
		if (this.pendingCoalesce) {
			this.pendingCoalesce = false;
			this.scheduleRender();
		} else {
			this.render();
		}
	}

	/** Coalesce high-frequency repaints (streaming tokens, spinner ticks) into
	 *  at most one frame per ~32ms so the UI never falls behind the model. */
	private scheduleRender(delay = this.streamDebounceMs): void {
		if (this.renderTimer) return;
		this.renderTimer = setTimeout(() => {
			this.renderTimer = null;
			this.render();
		}, delay);
	}

	/** Force a repaint (also clears the cached frame so the whole screen redraws). */
	repaint(): void {
		this.prevFrame = null;
		this.render();
	}

	// ---- SlashCommandHost implementation ----

	/** Append a system-line entry to the chat history. */
	appendSystemLine(text: string, render = true): void {
		this.appendHistory({ role: "system", text });
		if (render) this.render();
	}

	/** Clear the in-memory chat history. */
	clearHistory(): void {
		this.history = [];
		this.render();
		this.onClear?.();
	}

	/** Replace the on-screen conversation with a restored transcript (used by
	 *  `/resume`). Renders each user/assistant message as a boxed entry. */
	loadConversation(messages: { role: string; content: unknown; toolCallId?: string; toolName?: string; isError?: boolean; details?: unknown }[], render = true): void {
		this.history = [];
		this.streamingText = "";
		for (const m of messages) {
			if (m.role === "user") {
				const text = typeof m.content === "string" ? m.content : "";
				this.appendHistory({ role: "user", text });
			} else if (m.role === "assistant" && typeof m.content !== "string") {
				const parts: string[] = [];
				for (const c of m.content as any[]) {
					if (c?.type === "text") parts.push(c.text);
					if (c?.type === "toolCall") this.appendHistory({ role: "tool", text: formatToolCall(c.name, c.arguments), toolCallId: c.id, name: c.name, args: c.arguments, status: "running", body: "" });
				}
				const text = parts.join("");
				if (text) this.appendHistory({ role: "assistant", text });
			} else if (m.role === "toolResult") {
				const entry = m.toolCallId ? this.findToolEntry(m.toolCallId) : undefined;
				const result = { content: m.content, details: m.details };
				if (entry) {
					entry.status = m.isError ? "error" : "done";
					entry.result = result;
					entry.name = m.toolName ?? entry.name;
				} else if (m.toolName) {
					this.appendHistory({ role: "tool", text: m.toolName, toolCallId: m.toolCallId, name: m.toolName, args: {}, status: m.isError ? "error" : "done", result });
				}
			}
		}
		this.entryCache = new WeakMap();
		if (render) this.render();
	}

	getSetting(key: string): unknown {
		return this.getSettingHook?.(key);
	}

	setSetting(key: string, value: unknown): void {
		this.setSettingHook?.(key, value);
	}

	setTodos(todos: readonly TuiTodoItem[], render = true): void {
		this.todos = todos.map((todo) => ({ text: todo.text, status: todo.status }));
		if (render) this.render();
	}

	confirm(prompt: string): Promise<boolean> {
		const tty = (this.input as any).isTTY;
		if (tty === false) return Promise.resolve(false);
		return new Promise<boolean>((resolve) => {
			this.confirmationQueue.push({ prompt, resolve });
			this.advanceConfirmation();
		});
	}

	/** Submit a follow-up user prompt as if the user typed it. */
	async submitFollowup(text: string): Promise<void> {
		this.inputBuffer = text;
		await this.submit();
	}

	/** Quit the TUI. */
	quitTui(): void {
		this.quit();
	}

	// --- elapsed timer + spinner ---

	private startElapsedTimer(): void {
		this.stopElapsedTimer();
		// ~80ms tick drives the braille spinner in the status bar. With the
		// per-entry render cache the repaint is cheap (only the status row
		// changes, so the diff writes a single line).
		this.elapsedTimer = setInterval(() => this.render(), 80);
	}

	private stopElapsedTimer(): void {
		if (this.elapsedTimer) {
			clearInterval(this.elapsedTimer);
			this.elapsedTimer = null;
		}
	}

// --- internals ---

	private userText(message: AgentMessage): string {
		if (typeof message.content === "string") return message.content;
		return message.content
			.map((c) => (c.type === "text" ? c.text : c.type === "image" ? `[${c.mimeType}]` : ""))
			.join("\n");
	}

	private assistantText(message: AssistantMessage): string {
		const parts: string[] = [];
		for (const c of message.content) {
			if (c.type === "text") parts.push(renderAssistantText(c.text));
			else if (c.type === "thinking" && this.showThinking) parts.push(`[thinking: ${c.thinking}]`);
		}
		return parts.join("");
	}

	private appendHistory(entry: TuiHistoryEntry): void {
		this.history.push(entry);
	}

	private findToolEntry(toolCallId: string): TuiHistoryEntry | undefined {
		for (let i = this.history.length - 1; i >= 0; i--) {
			const entry = this.history[i]!;
			if (entry.role === "tool" && entry.toolCallId === toolCallId) return entry;
		}
		return undefined;
	}

	private advanceConfirmation(): void {
		if (this.activeConfirmation) return;
		this.activeConfirmation = this.confirmationQueue.shift();
		if (this.activeConfirmation) this.render();
	}

	private answerConfirmation(value: boolean): void {
		const active = this.activeConfirmation;
		if (!active) return;
		this.activeConfirmation = undefined;
		active.resolve(value);
		this.advanceConfirmation();
		this.render();
	}

	private toggleLatestTool(): void {
		for (let i = this.history.length - 1; i >= 0; i--) {
			const entry = this.history[i]!;
			if (entry.role === "tool" && entry.name) {
				entry.expanded = !entry.expanded;
				this.entryCache.delete(entry);
				this.render();
				return;
			}
		}
	}

	private handleInput(chunk: Buffer): void {
		const s = chunk.toString();
		if (this.activeConfirmation) {
			if (s === "y" || s === "Y" || s === "\r" || s === "\n") this.answerConfirmation(true);
			else if (s === "n" || s === "N" || s === "\x1b" || s === "\x03") this.answerConfirmation(false);
			return;
		}
		if (s === "\x0f") {
			this.toggleLatestTool();
			return;
		}
		if (this.mode === "selector") {
			this.handleSelectorInput(s);
			return;
		}
		// Reverse-history search swallows most keys while active.
		if (this.searchActive) {
			this.handleSearchInput(s);
			return;
		}
		// Ctrl+C: interrupt a running operation, else clear the prompt; a
		// second press (when the prompt is empty) quits — like Claude Code.
		if (s === "\x03") {
			if (this.busy) {
				this.status = "interrupted";
				this.onInterrupt?.();
				this.busy = false;
				this.stopElapsedTimer();
				this.render();
				return;
			}
			if (this.inputBuffer.length > 0) {
				this.inputBuffer = "";
				this.cursorPos = 0;
				this.showSuggestions = false;
				this.suggestions = [];
				this.render();
				return;
			}
			this.quit();
			return;
		}
		if (s === "\x1b") {
			this.quit();
			return;
		}
		// Ctrl+R — reverse search command history.
		if (s === "\x12") {
			this.beginSearch();
			return;
		}
		// Tab → complete the current slash command (if a unique prefix matches).
		if (s === "\t") {
			this.completeSlashCommand();
			return;
		}
		if (s === "\r" || s === "\n") {
			this.showSuggestions = false;
			this.suggestions = [];
			// Paste detection: if there's already buffered paste text or
			// a pending paste timer, keep buffering. Otherwise start a
			// short timer — if more input arrives before it fires, we
			// treat the whole thing as a paste.
			if (this.pasteTimer) {
				// Timer running → more input arrived → it's a paste.
				this.pasteBuffer += this.inputBuffer + "\n";
				this.inputBuffer = "";
				this.render();
			} else if (this.inputBuffer.length > 0) {
				// First Enter in a potential paste — start timer.
				this.pasteBuffer = this.inputBuffer + "\n";
				this.inputBuffer = "";
				this.render();
				this.pasteTimer = setTimeout(() => {
					this.pasteTimer = null;
					const full = this.pasteBuffer.trim();
					this.pasteBuffer = "";
					if (full) {
						this.inputBuffer = full;
						void this.submit();
					}
				}, 50);
			} else {
				// Empty input + no paste buffer → ignore or submit empty.
				void this.submit();
			}
			return;
		}
		// Backspace → delete char before cursor.
		if (s === "\x7f" || s === "\b") {
			if (this.cursorPos > 0) {
				this.inputBuffer = this.inputBuffer.slice(0, this.cursorPos - 1) + this.inputBuffer.slice(this.cursorPos);
				this.cursorPos -= 1;
			}
			this.updateSuggestions();
			this.render();
			return;
		}
		// Ctrl+D → delete char after cursor (or quit when input is empty).
		if (s === "\x04") {
			if (this.inputBuffer.length > 0 && this.cursorPos < this.inputBuffer.length) {
				this.inputBuffer = this.inputBuffer.slice(0, this.cursorPos) + this.inputBuffer.slice(this.cursorPos + 1);
				this.updateSuggestions();
				this.render();
				return;
			}
			this.quit();
			return;
		}
		// Ctrl+A / Ctrl+E — start / end of line.
		if (s === "\x01") {
			this.cursorPos = 0;
			this.render();
			return;
		}
		if (s === "\x05") {
			this.cursorPos = this.inputBuffer.length;
			this.render();
			return;
		}
		// Ctrl+U → kill to start. Ctrl+K → kill to end. Ctrl+W → kill word.
		if (s === "\x15") {
			this.inputBuffer = this.inputBuffer.slice(this.cursorPos);
			this.cursorPos = 0;
			this.updateSuggestions();
			this.render();
			return;
		}
		if (s === "\x0b") {
			this.inputBuffer = this.inputBuffer.slice(0, this.cursorPos);
			this.render();
			return;
		}
		if (s === "\x17") {
			const before = this.inputBuffer.slice(0, this.cursorPos);
			const match = /(\s*)(\S+)\s*$/.exec(before);
			if (match) {
				const keepLen = before.length - match[0].length;
				this.inputBuffer = this.inputBuffer.slice(0, keepLen) + this.inputBuffer.slice(this.cursorPos);
				this.cursorPos = keepLen;
				this.render();
			}
			return;
		}
		if (s === "\x0c") {
			this.repaint();
			return;
		}
		if (s.startsWith("\x1b[")) {
			this.handleAnsiEscape(s);
			return;
		}
		if (s >= " " && !s.startsWith("\x1b")) {
			// Any new typing clears the history cursor (we're now editing a
			// fresh prompt, not browsing past ones).
			this.inputHistoryCursor = -1;
			this.inputHistoryDraft = "";
			// Cancel paste timer if user types a non-Enter char.
			if (this.pasteTimer && s !== "\r" && s !== "\n") {
				clearTimeout(this.pasteTimer);
				this.pasteTimer = null;
				// Flush any buffered paste text back into the input.
				if (this.pasteBuffer) {
					this.inputBuffer = this.inputBuffer.slice(0, this.cursorPos) + this.pasteBuffer.replace(/\n$/, "") + this.inputBuffer.slice(this.cursorPos);
					this.cursorPos += this.pasteBuffer.replace(/\n$/, "").length;
					this.pasteBuffer = "";
				}
			}
			// Cursor-aware insertion.
			this.inputBuffer = this.inputBuffer.slice(0, this.cursorPos) + s + this.inputBuffer.slice(this.cursorPos);
			this.cursorPos += s.length;
			this.updateSuggestions();
			this.render();
		}
	}

	/** Handle ANSI CSI sequences: arrow keys, Home/End, Delete. */
	private handleAnsiEscape(s: string): void {
		if (s === "\x1b[A" || s === "\x1b[B") {
			// When the suggestion popup is open, ↑/↓ navigates it.
			if (this.showSuggestions && this.suggestions.length > 0) {
				const delta = s === "\x1b[A" ? -1 : 1;
				const len = this.suggestions.length;
				this.suggestionCursor = (this.suggestionCursor + delta + len) % len;
				this.inputBuffer = this.suggestions[this.suggestionCursor]!.name;
				this.cursorPos = this.inputBuffer.length;
				this.render();
				return;
			}
			// Otherwise ↑/↓ walk input history when editing, else scroll.
			const wantHistory = this.inputBuffer.length > 0 || this.inputHistoryCursor !== -1;
			if (wantHistory) {
				if (s === "\x1b[A") {
					if (this.inputHistoryCursor === -1) {
						if (this.inputHistory.length === 0) return;
						this.inputHistoryCursor = this.inputHistory.length - 1;
						this.inputHistoryDraft = this.inputBuffer;
					} else if (this.inputHistoryCursor > 0) {
						this.inputHistoryCursor -= 1;
					} else {
						return;
					}
				} else {
					if (this.inputHistoryCursor === -1) return;
					if (this.inputHistoryCursor < this.inputHistory.length - 1) {
						this.inputHistoryCursor += 1;
					} else {
						this.inputHistoryCursor = -1;
						this.inputBuffer = this.inputHistoryDraft;
						this.cursorPos = this.inputBuffer.length;
						this.render();
						return;
					}
				}
				this.inputBuffer = this.inputHistory[this.inputHistoryCursor] ?? "";
				this.cursorPos = this.inputBuffer.length;
				this.render();
			} else {
				this.scroll(s === "\x1b[A" ? -1 : 1);
			}
			return;
		}
		if (s === "\x1b[C") {
			// → move cursor right.
			if (this.cursorPos < this.inputBuffer.length) {
				this.cursorPos += 1;
				this.render();
			}
			return;
		}
		if (s === "\x1b[D") {
			// ← move cursor left.
			if (this.cursorPos > 0) {
				this.cursorPos -= 1;
				this.render();
			}
			return;
		}
		if (s === "\x1b[H" || s === "\x1b[1~") {
			// Home → start of line.
			this.cursorPos = 0;
			this.render();
			return;
		}
		if (s === "\x1b[F" || s === "\x1b[4~") {
			// End → end of line.
			this.cursorPos = this.inputBuffer.length;
			this.render();
			return;
		}
		if (s === "\x1b[3~") {
			// Delete key → delete char after cursor.
			if (this.cursorPos < this.inputBuffer.length) {
				this.inputBuffer = this.inputBuffer.slice(0, this.cursorPos) + this.inputBuffer.slice(this.cursorPos + 1);
				this.updateSuggestions();
				this.render();
			}
			return;
		}
		// Ignore all other escape sequences.
	}

	/** Start a reverse-history search (Ctrl+R). */
	private beginSearch(): void {
		this.searchDraft = this.inputBuffer;
		this.searchActive = true;
		this.searchQuery = "";
		this.searchMatches = [];
		this.searchCursor = -1;
		this.renderChat();
	}

	/** Handle keys while reverse-search is active. */
	private handleSearchInput(s: string): void {
		if (s === "\x12") {
			// Ctrl+R again → next older match.
			if (this.searchMatches.length > 0 && this.searchCursor < this.searchMatches.length - 1) {
				this.searchCursor += 1;
			}
			this.renderChat();
			return;
		}
		if (s === "\r" || s === "\n") {
			this.acceptSearch();
			return;
		}
		if (s === "\x03" || s === "\x1b") {
			// Cancel → restore the draft.
			this.searchActive = false;
			this.inputBuffer = this.searchDraft;
			this.cursorPos = this.inputBuffer.length;
			this.renderChat();
			return;
		}
		if (s === "\x7f" || s === "\b" || s === "\x04") {
			// Backspace / Ctrl+D → edit the query.
			if (s === "\x7f" || s === "\b") {
				this.searchQuery = this.searchQuery.slice(0, -1);
			}
			this.recomputeSearch();
			this.renderChat();
			return;
		}
		if (s >= " " && !s.startsWith("\x1b")) {
			this.searchQuery += s;
			this.recomputeSearch();
			this.renderChat();
		}
	}

	/** Recompute matches for the current search query (most recent first). */
	private recomputeSearch(): void {
		const q = this.searchQuery.toLowerCase();
		this.searchMatches = this.inputHistory
			.slice()
			.reverse()
			.filter((h) => h.toLowerCase().includes(q))
			.slice(0, 50);
		this.searchCursor = this.searchMatches.length > 0 ? 0 : -1;
	}

	/** Accept the current (or draft) search result back into the input. */
	private acceptSearch(): void {
		this.searchActive = false;
		const chosen = this.searchCursor >= 0 ? this.searchMatches[this.searchCursor] : undefined;
		this.inputBuffer = chosen ?? this.searchDraft;
		this.cursorPos = this.inputBuffer.length;
		this.renderChat();
	}

	/** Recompute the slash-command suggestion popup based on the current
	 * input buffer. Shows suggestions when the buffer starts with `/` and
	 * matches at least one command. */
	private updateSuggestions(): void {
		const trimmed = this.inputBuffer.trimStart();
		if (!trimmed.startsWith("/")) {
			this.showSuggestions = false;
			this.suggestions = [];
			return;
		}
		// Extract the word after the slash (no spaces).
		const match = /^\/(\S*)/.exec(trimmed);
		if (!match) {
			this.showSuggestions = false;
			this.suggestions = [];
			return;
		}
		const query = match[1] ?? "";
		const matches = findCommands(query, 10);
		if (matches.length === 0) {
			this.showSuggestions = false;
			this.suggestions = [];
		} else {
			// Show the popup whenever we have an unclosed `/` command, even if the
			// query is empty (i.e. the user just typed `/`).
			this.suggestions = matches.map((c) => ({ name: c.name, description: c.description }));
			this.showSuggestions = matches.length > 0;
			// Keep the highlight within bounds (e.g. after a filter narrows the list).
			if (this.suggestionCursor >= this.suggestions.length) {
				this.suggestionCursor = Math.max(0, this.suggestions.length - 1);
			}
		}
	}

	/** Tab-complete the current slash-command input. If there's a unique
	 * common prefix or a single match, fill in the command name. */
	private completeSlashCommand(): void {
		if (this.suggestions.length === 0) {
			this.repaint();
			return;
		}
		if (this.suggestions.length === 1) {
			const name = this.suggestions[0]!.name;
			this.inputBuffer = name;
			this.cursorPos = name.length;
			this.showSuggestions = false;
			this.suggestions = [];
			this.render();
			return;
		}
		// Find the longest common prefix of all matching command names.
		const names = this.suggestions.map((s) => s.name.slice(1)); // strip leading /
		const prefix = longestCommonPrefix(names);
		if (prefix) {
			this.inputBuffer = `/${prefix}`;
			this.cursorPos = this.inputBuffer.length;
			this.render();
		}
	}

	/** Input handling while the `/model` selector overlay is open. */
	private handleSelectorInput(s: string): void {
		if (s === "\x1b" || s === "\x03") {
			this.closeSelector(); // Esc / Ctrl+C cancels without changing model
			return;
		}
		if (this.selectorLoading) {
			// Ignore everything except cancel while the list loads.
			return;
		}
		if (s === "\r" || s === "\n") {
			void this.confirmSelector();
			return;
		}
		if (s === "\x7f" || s === "\b") {
			this.selectorFilter = this.selectorFilter.slice(0, -1);
			this.selectorCursor = 0;
			this.recomputeFiltered();
			this.render();
			return;
		}
		if (s === "\x1b[A") {
			this.moveCursor(-1);
			return;
		}
		if (s === "\x1b[B") {
			this.moveCursor(1);
			return;
		}
		if (s.startsWith("\x1b[")) return; // ignore other escape sequences
		if (s >= " ") {
			this.selectorFilter += s;
			this.selectorCursor = 0;
			this.recomputeFiltered();
			this.render();
		}
	}

	private recomputeFiltered(): void {
		this.selectorFiltered = filterModels(this.selectorAll, this.selectorFilter);
		if (this.selectorCursor >= this.selectorFiltered.length) {
			this.selectorCursor = Math.max(0, this.selectorFiltered.length - 1);
		}
	}

	private moveCursor(delta: number): void {
		if (this.selectorFiltered.length === 0) return;
		this.selectorCursor = Math.max(
			0,
			Math.min(this.selectorFiltered.length - 1, this.selectorCursor + delta),
		);
		this.render();
	}

	private async openSelector(): Promise<void> {
		this.mode = "selector";
		this.selectorAll = [];
		this.selectorFiltered = [];
		this.selectorFilter = "";
		this.selectorCursor = 0;
		this.selectorLoading = true;
		this.render();
		try {
			this.selectorAll = (await this.onListModels?.()) ?? [];
		} catch {
			this.selectorAll = [];
		}
		// Fall back to the default model list if the network returned
		// nothing (e.g. no API key, no network). The current model is
		// always at the top.
		if (this.selectorAll.length === 0 && this.defaultModels.length > 0) {
			this.selectorAll = this.defaultModels;
		}
		if (this.model && !this.selectorAll.includes(this.model)) {
			this.selectorAll = [this.model, ...this.selectorAll];
		}
		this.selectorLoading = false;
		this.recomputeFiltered();
		this.render();
	}

	private closeSelector(): void {
		this.mode = "chat";
		this.selectorAll = [];
		this.selectorFiltered = [];
		this.selectorFilter = "";
		this.selectorCursor = 0;
		this.selectorLoading = false;
		this.render();
	}

	private async confirmSelector(): Promise<void> {
		const id = this.selectorFiltered[this.selectorCursor];
		this.closeSelector();
		if (!id) return;
		try {
			await this.onSelectModel?.(id);
		} catch (err) {
			this.appendHistory({
				role: "system",
				text: `[error] ${err instanceof Error ? err.message : String(err)}`,
			});
		}
	}

	private scroll(direction: number): void {
		const [cols, rows] = this.getSize();
		const content = computeContentLines(this.history, this.streamingText, cols);
		const usable = Math.max(1, rows - 2);
		const maxScroll = Math.max(0, content.length - usable);
		this.userScroll = Math.min(0, Math.max(-maxScroll, this.userScroll + direction));
		this.render();
	}

	private async submit(): Promise<void> {
		const text = this.inputBuffer.trim();
		this.cursorPos = 0;
		if (!text || this.busy) {
			this.inputBuffer = "";
			this.render();
			return;
		}
		this.inputBuffer = "";
		// Push to input history (deduplicated consecutive duplicates).
		const last = this.inputHistory[this.inputHistory.length - 1];
		if (last !== text) {
			this.inputHistory.push(text);
			// Cap history at 200 entries.
			if (this.inputHistory.length > 200) {
				this.inputHistory = this.inputHistory.slice(-200);
			}
		}
		this.inputHistoryCursor = -1;
		this.inputHistoryDraft = "";

		// Slash commands (only when not busy). Unknown `/x` falls through as a
		// normal message below. Built-in /model and /clear are always handled
		// here so the TUI works even if the host didn't supply onSlashCommand.
		if (text === "/model") {
			this.render();
			void this.openSelector();
			return;
		}
		if (text === "/clear") {
			this.history = [];
			this.render();
			this.onClear?.();
			return;
		}
		if (this.onSlashCommand) {
			const result = await this.onSlashCommand(text);
			if (result.handled) {
				if (result.clearHistory) {
					this.history = [];
				}
				if (result.message) {
					this.appendHistory({ role: "system", text: result.message });
				}
				if (result.quit) {
					this.quit();
					return;
				}
				if (result.submitFollowUp?.trim()) {
					await this.submitText(result.submitFollowUp.trim());
				} else {
					this.render();
				}
				return;
			}
		}

		await this.submitText(text);
	}

	private async submitText(text: string): Promise<void> {
		if (!text || this.busy) return;
		this.busy = true;
		this.status = "thinking…";
		this.busyStartTime = Date.now();
		this.startElapsedTimer();
		this.render();
		try {
			await this.onSubmit(text);
		} catch (err) {
			this.appendHistory({ role: "system", text: `[error] ${err instanceof Error ? err.message : String(err)}` });
		} finally {
			this.busy = false;
			this.status = "";
			this.stopElapsedTimer();
			this.render();
		}
	}

	private quit(): void {
		if (this.pasteTimer) { clearTimeout(this.pasteTimer); this.pasteTimer = null; }
		if (this.renderTimer) { clearTimeout(this.renderTimer); this.renderTimer = null; }
		this.stopElapsedTimer();
		(process.stdout as any)?.removeListener?.("resize", this.onResize);
		this.input.removeListener("data", this.onData);
		try {
			this.input.setRawMode?.(false);
		} catch {
			// Non-TTY stdin — nothing to restore.
		}
		this.input.pause();
		this.write(`${ESC}[?25h`); // show cursor
		this.write(`${ESC}[?1049l`); // leave alt screen
		this.onQuit?.();
		this.resolveRun?.();
	}

	private render(): void {
		if (this.mode === "selector") {
			this.renderSelector();
			return;
		}
		this.renderChat();
	}

	private renderChat(): void {
		const [rawCols, rawRows] = this.getSize();
		// Hard floors: a degenerate size must never crash the frame math.
		const cols = Math.max(20, rawCols);
		const rows = Math.max(4, rawRows);
		// Never write into the LAST column. On Windows, stdout.columns can be
		// stale/wider than the real console; a full-width line then wraps, and
		// a wrap reaching the bottom-right cell scrolls the whole screen (the
		// missing banner top + `you›` glued after `/help` in todo.txt).
		// Reserving one column makes every wrap path impossible.
		const safeCols = Math.max(20, cols - 1);
		const sugCount = this.showSuggestions && this.suggestions.length > 0
			? Math.min(this.suggestions.length, 6)
			: 0;
		const modalCount = this.activeConfirmation ? Math.min(4, rows - 2) : 0;
		const todoCount = Math.min(this.todos.length, Math.max(0, Math.min(6, rows - 3 - sugCount - modalCount)));
		const reserved = 2 + sugCount + todoCount + modalCount;
		const usable = Math.max(1, rows - reserved);

		const content = this.contentLines(safeCols);
		const autoOffset = Math.max(0, content.length - usable);
		const offset = Math.min(autoOffset, autoOffset + this.userScroll);
		// Clamp every line to the safe width so nothing can ever wrap.
		const visible = content
			.slice(offset)
			.map((l) => truncateToWidth(l, safeCols));

		// --- Status bar (Pi Harness style) ---
		// Left: model name, tokens, context %. Right: spinner + elapsed, hint.
		const elapsed = this.busy && this.busyStartTime > 0
			? formatElapsed(Date.now() - this.busyStartTime)
			: "";
		const spinner = SPINNER_FRAMES[Math.floor(Date.now() / 80) % SPINNER_FRAMES.length]!;

		const tokenStr = this.lastUsage.total > 0
			? `${this.lastUsage.input.toLocaleString()}↑ ${this.lastUsage.output.toLocaleString()}↓`
			: "";

		const contextPct = this.contextWindow > 0 && this.lastUsage.input > 0
			? `${Math.round((this.lastUsage.input / this.contextWindow) * 100)}%`
			: "";

		// Build status segments.
		const leftParts: string[] = [`${BOLD}${this.model}${RESET}`];
		if (tokenStr) leftParts.push(tokenStr);
		if (contextPct) leftParts.push(contextPct);
		const leftSide = leftParts.join(` ${DIM}•${RESET} `);

		const rightParts: string[] = [`${DIM}/help${RESET}`];
		if (elapsed) {
			rightParts.unshift(`${CYAN}${spinner}${RESET} ${DIM}${elapsed}${RESET}`);
		}
		const rightSide = rightParts.join(` ${DIM}•${RESET} `);

		// Pad right side to fill the row minus the reserved last column.
		const leftWidth = visibleWidth(leftSide);
		const rightWidth = visibleWidth(rightSide);
		const gaps = Math.max(1, safeCols - leftWidth - rightWidth);
		const statusLine = truncateToWidth(
			`${REVERSE}${leftSide}${" ".repeat(gaps)}${rightSide}${RESET}`,
			safeCols,
		);

		// Input line: prefix + buffer (cursor-aware).
		let inputLine: string;
		let inputCursorCol: number;
		if (this.searchActive) {
			const label = `${CYAN}(reverse-i-search)\`${RESET}${this.searchQuery}${CYAN}\`${RESET}${DIM}${this.searchCursor >= 0 ? `: ${this.searchMatches[this.searchCursor]}` : ": (no match)"}${RESET}`;
			inputLine = truncateToWidth(label, safeCols);
			inputCursorCol = 1 + visibleWidth(`(reverse-i-search)\``) + visibleWidth(this.searchQuery);
		} else {
			inputLine = truncateToWidth(`${USER_PREFIX}${this.inputBuffer}${RESET}`, safeCols);
			inputCursorCol = 1 + USER_PREFIX_VW + visibleWidth(this.inputBuffer.slice(0, this.cursorPos));
		}
		const suggestionLines = this.renderSuggestions(safeCols).map((l) => truncateToWidth(l, safeCols));
		const todoLines = renderTodos(this.todos, safeCols, todoCount);
		const modalLines = this.activeConfirmation
			? renderBox(`${this.activeConfirmation.prompt}\n${BOLD}y/Enter${RESET} yes  ${BOLD}n/Esc${RESET} no`, safeCols, "confirm", YELLOW).slice(0, modalCount)
			: [];
		const frame = [...visible, ...todoLines, ...modalLines, statusLine, inputLine, ...suggestionLines].slice(0, rows);

		this.writeFrame(frame, safeCols, rows, undefined, inputCursorCol);
	}

	/** Wrapped content lines with a per-entry cache: committed history entries
	 *  are only re-rendered when they're appended/changed or the width changes,
	 *  so per-token repaints only re-wrap the in-flight message. */
	private contentLines(width: number): string[] {
		if (width !== this.cacheWidth) {
			this.entryCache = new WeakMap();
			this.cacheWidth = width;
		}
		const out: string[] = [];
		for (const entry of this.history) {
			const cached = this.entryCache.get(entry);
			if (cached) {
				out.push(...cached);
				continue;
			}
			const lines = renderHistoryEntry(entry, width);
			this.entryCache.set(entry, lines);
			out.push(...lines);
		}
		if (this.streamingText.length > 0) {
			// The in-flight reply lives in its own open-bottomed box; the block
			// cursor shows tokens are arriving (Claude Code style).
			out.push(...renderBox(`${this.streamingText}${CURSOR_BLOCK}`, width, "friday", MAGENTA, true));
		}
		return out;
	}

	/** Render the slash-command suggestion popup beneath the input line. The
	 *  highlighted (selected) row is given reverse video; ↑/↓ moves the
	 *  selection. */
	private renderSuggestions(cols: number): string[] {
		if (!this.showSuggestions || this.suggestions.length === 0) return [];
		const max = 6; // cap visible height
		const items = this.suggestions.slice(0, max);
		const nameWidth = Math.min(
			cols,
			Math.max(...items.map((s) => s.name.length)),
		);
		return items.map((s, i) => {
			const selected = i === this.suggestionCursor;
			const dim = selected ? `${REVERSE}` : (i === 0 ? YELLOW : DIM);
			const desc = s.description ?? "";
			const line = `${dim}${s.name.padEnd(nameWidth)} ${RESET}${desc.slice(0, Math.max(0, cols - nameWidth - 1))}`;
			return selected ? `${line}${RESET}` : line;
		});
	}

	/** Draw the `/model` selector: filter box on top, windowed list, hint at bottom. */
	private renderSelector(): void {
		const [rawCols, rawRows] = this.getSize();
		// Reserve the last column — never write into it (scroll/wrap hazard).
		const cols = Math.max(21, rawCols - 1);
		const rows = Math.max(4, rawRows);
		const hintLine = `${DIM}↑↓ move • Enter select • Esc cancel${RESET}`;
		const filterLine = truncateToWidth(
			`${YELLOW}/model › ${RESET}${this.selectorFilter}${RESET}`,
			cols,
		);
		const usable = Math.max(1, rows - 2); // minus filter + hint

		let listLines: string[];
		if (this.selectorLoading) {
			listLines = [`${DIM}loading models…${RESET}`];
		} else if (this.selectorFiltered.length === 0) {
			listLines = [`${DIM}(no models match "${this.selectorFilter}")${RESET}`];
		} else {
			listLines = visibleSelector(
				this.selectorFiltered,
				this.selectorCursor,
				usable,
				this.model,
			).lines;
		}

		// Clamp every line — long model ids must never wrap the frame.
		const frame = [
			filterLine,
			...listLines.map((l) => truncateToWidth(l, cols)),
			truncateToWidth(hintLine, cols),
		];
		// Cursor sits at the end of the filter line (row 1), clamped to the
		// safe width so it can never be pushed into the last column.
		const cursorCol = Math.min(
			cols,
			1 + visibleWidth("/model › ") + visibleWidth(this.selectorFilter),
		);
		this.writeFrame(frame, cols, rows, 1, cursorCol);
	}

	private writeFrame(frame: string[], _cols: number, rows: number, cursorRow?: number, cursorCol?: number): void {
		if (!this.prevFrame) {
			this.write(`${ESC}[2J`); // first paint: clear screen
		}
		const { changed, cleared } = diffFrame(this.prevFrame, frame);
		for (const i of changed) {
			this.write(`${ESC}[${i + 1};1H${ESC}[2K${frame[i] ?? ""}`);
		}
		for (const i of cleared) {
			this.write(`${ESC}[${i + 1};1H${ESC}[2K`);
		}

		// Position the hardware cursor. In chat mode it tracks the input line;
		// callers may override (e.g. the selector puts it on the filter box).
		// Both row and column are clamped so the cursor can never be pushed
		// past the frame or into the last column (wrap/scroll hazard).
		const row = Math.min(cursorRow ?? Math.min(frame.length, rows), rows);
		const col = Math.min(_cols, cursorCol ?? 1 + USER_PREFIX_VW + visibleWidth(this.inputBuffer));
		this.write(`${ESC}[${row};${col}H${ESC}[?25h`);

		this.prevFrame = frame;
	}

	/** Update the displayed model (called after a `/model` selection). */
	setModel(modelId: string): void {
		this.model = modelId;
		this.render();
	}

	applySettings(settings: { showThinking?: boolean; streamDebounceMs?: number }): void {
		if (settings.showThinking !== undefined) this.showThinking = settings.showThinking;
		if (settings.streamDebounceMs !== undefined && Number.isFinite(settings.streamDebounceMs)) {
			this.streamDebounceMs = Math.max(0, settings.streamDebounceMs);
		}
		this.render();
	}
}

/** Format milliseconds as a compact elapsed string: "12s" or "1m 23s". */
function formatElapsed(ms: number): string {
	const secs = Math.floor(ms / 1000);
	if (secs < 60) return `${secs}s`;
	const mins = Math.floor(secs / 60);
	const rem = secs % 60;
	return `${mins}m ${rem}s`;
}

function safeStringify(value: unknown): string {
	try {
		const s = JSON.stringify(value);
		return s && s.length > 120 ? `${s.slice(0, 120)}…` : s ?? "";
	} catch {
		return "";
	}
}

/** Format a tool call for display: the tool name plus its most relevant
 *  argument (the command for bash, the path for file tools, the query for
 *  websearch), collapsed to one short line. Raw argument JSON is only used
 *  for unknown tools — the chat must not be flooded with file contents or
 *  argument dumps. */
export function formatToolCall(name: string, args: unknown): string {
	const a = (args ?? {}) as Record<string, unknown>;
	const pick = (...keys: string[]): string | undefined => {
		for (const k of keys) {
			const v = a[k];
			if (typeof v === "string" && v.trim().length > 0) return v;
		}
		return undefined;
	};
	let summary: string;
	switch (name) {
		case "bash":
		case "calculator":
			summary = pick("command", "expression") ?? "";
			break;
		case "read":
		case "write":
		case "edit":
			summary = pick("path") ?? "";
			break;
		case "glob":
			summary = pick("pattern") ?? "";
			break;
		case "grep":
			summary = pick("pattern") ?? "";
			break;
		case "websearch":
			summary = pick("query") ?? "";
			break;
		default: {
			const s = safeStringify(a);
			summary = s && s !== "{}" ? s : "";
		}
	}
	const oneLine = summary.split("\n").map((l) => l.trim()).filter(Boolean).join(" ⏎ ");
	const clipped = oneLine.length > 80 ? `${oneLine.slice(0, 77)}…` : oneLine;
	return clipped ? `${name} ${clipped}` : name;
}

function firstLines(text: string, maxLines: number, maxChars: number): string {
	if (!text) return "";
	const lines = text.split("\n").slice(0, maxLines).map((l) => l.trimEnd());
	let out = lines.join("\n");
	if (out.length > maxChars) out = `${out.slice(0, maxChars)}…`;
	return out;
}

/** Build a short human summary of a tool result from its metadata — the
 *  opposite of dumping content into the chat. File reads report line
 *  counts, searches report match counts, bash reports its exit code plus
 *  at most a 3-line output preview. */
export function summarizeToolResult(name: string, result: unknown, isError: boolean): string {
	const res = (result ?? {}) as { content?: { type: string; text?: string }[]; details?: Record<string, unknown> };
	const text = (res.content ?? [])
		.filter((c) => c?.type === "text")
		.map((c) => c.text ?? "")
		.join("\n")
		.trim();
	const details = res.details ?? {};
	if (isError) {
		return `error: ${firstLines(text || "failed", 1, 160)}`;
	}
	switch (name) {
		case "bash": {
			const timedOut = details.timedOut === true;
			const code = typeof details.code === "number" ? details.code : 0;
			const body = text
				.split("\n")
				.filter((l) => !/^\[(exit code|command timed out)/.test(l.trim()))
				.join("\n")
				.trim();
			const head = timedOut ? "timed out" : `exit ${code}`;
			const preview = firstLines(body, 3, 240);
			return preview ? `${head}\n${preview}` : head;
		}
		case "read": {
			if (typeof details.totalLines === "number") {
				return `${details.totalLines} lines`;
			}
			if (typeof details.returnedBytes === "number") {
				return `${details.returnedBytes} bytes (truncated)`;
			}
			return "done";
		}
		case "glob":
		case "grep": {
			const m = details.matches;
			if (typeof m === "number") return `${m} match${m === 1 ? "" : "es"}`;
			return "done";
		}
		case "websearch": {
			const r = details.results;
			const src = typeof details.source === "string" ? ` (${details.source})` : "";
			if (typeof r === "number") return `${r} result${r === 1 ? "" : "s"}${src}`;
			return "done";
		}
		case "write": {
			const b = details.bytes;
			if (typeof b === "number") return `wrote ${b} bytes`;
			return "done";
		}
		case "edit":
			return "edited";
		default:
			return firstLines(text, 1, 120) || "done";
	}
}

/** Find the longest common prefix of a list of strings. */
function longestCommonPrefix(strs: string[]): string {
	if (strs.length === 0) return "";
	let prefix = strs[0]!;
	for (let i = 1; i < strs.length; i++) {
		while (!strs[i]!.startsWith(prefix)) {
			prefix = prefix.slice(0, -1);
			if (!prefix) return "";
		}
	}
	return prefix;
}
