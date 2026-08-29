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
import { renderMarkdown, renderMarkdownColored } from "./markdown.ts";
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
			out.push(line.spans.map((s) => s.text).join(""));
		} else {
			out.push(line.spans.map((s) => s.text).join(""));
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

export interface TuiHistoryEntry {
	role: TuiRole;
	text: string;
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
	const segmenter = (globalThis as any).Intl?.Segmenter as
		| (new (l?: string, o?: { granularity: "grapheme" }) => {
				segment: (s: string) => Iterable<{ segment: string; index: number }>;
		  })
		| undefined;
	if (segmenter) {
		const iter = new segmenter("en", { granularity: "grapheme" }).segment(s);
		let width = 0;
		let cut = s.length;
		let progressed = false;
		for (const { segment, index } of iter) {
			const w = graphemeWidth(segment);
			if (width + w > max) {
				// If not even a single grapheme fits, emit it anyway so the
				// caller always makes forward progress. Without this guard a
				// 2-column emoji at max=1 (or a 6-column ZWJ family at max=1)
				// yields head="" forever → infinite loop → frozen TUI + OOM.
				cut = progressed ? index : index + segment.length;
				break;
			}
			width += w;
			progressed = true;
		}
		return { head: s.slice(0, cut), tail: s.slice(cut) };
	}
	// Fallback: char-by-char.
	let width = 0;
	let cut = s.length;
	let progressed = false;
	for (let i = 0; i < s.length; ) {
		const cp = s.codePointAt(i)!;
		const size = cp > 0xffff ? 2 : 1;
		const w = cp > 0xffff ? 2 : 1;
		if (width + w > max) {
			// Same progress guarantee as the segmenter path above.
			cut = progressed ? i : i + size;
			break;
		}
		width += w;
		i += size;
		progressed = true;
	}
	return { head: s.slice(0, cut), tail: s.slice(cut) };
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
	let width = 0;
	let inEscape = false;

	// Quick scan: strip ANSI escapes first so the segmenter doesn't see
	// them. Keep the string in order so emoji ZWJ sequences still group
	// correctly.
	const stripped: string[] = [];
	for (let i = 0; i < s.length; i++) {
		const code = s.charCodeAt(i);
		if (inEscape) {
			if (code === 0x6d /* 'm' */) inEscape = false;
			continue;
		}
		if (code === 0x1b /* ESC */) {
			inEscape = true;
			continue;
		}
		stripped.push(s[i]!);
	}
	const text = stripped.join("");

	const segmenter = (globalThis as any).Intl?.Segmenter as
		| (new (l?: string, o?: { granularity: "grapheme" }) => {
				segment: (s: string) => Iterable<{ segment: string }>;
		  })
		| undefined;
	if (segmenter) {
		const iter = new segmenter("en", { granularity: "grapheme" }).segment(text);
		for (const { segment } of iter) {
			width += graphemeWidth(segment);
		}
		return width;
	}

	// Fallback: per-code-unit count (1 each) — wrong for emoji, but stable.
	for (let i = 0; i < text.length; i++) width += 1;
	return width;
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
			return wrapText(entry.text, width, TOOL_PREFIX);
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
	if (visibleWidth(s) <= max) return s;
	let width = 0;
	let inEscape = false;
	let sawSgr = false;
	let end = s.length;
	for (let i = 0; i < s.length; i++) {
		const code = s.charCodeAt(i);
		if (inEscape) {
			if (code === 0x6d /* 'm' */) {
				inEscape = false;
				sawSgr = true;
			}
			continue;
		}
		if (code === 0x1b /* ESC */) {
			inEscape = true;
			continue;
		}
		const cp = s.codePointAt(i)!;
		const size = cp > 0xffff ? 2 : 1;
		const w = codepointWidth(cp);
		if (width + w > max) {
			end = i;
			break;
		}
		width += w;
		if (size === 2) i++; // skip low surrogate
	}
	let out = s.slice(0, end);
	if (sawSgr && !out.endsWith(RESET)) out += RESET;
	return out;
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
	/** Optional: parse a slash command. If it returns a result, the TUI
	 *  applies the result instead of submitting to the LLM. The default
	 *  is to only handle the built-in /model and /clear (preserving
	 *  current behavior). */
	onSlashCommand?: (
		input: string,
	) => Promise<{ handled: boolean; message?: string; clearHistory?: boolean; quit?: boolean }> | { handled: boolean; message?: string; clearHistory?: boolean; quit?: boolean };
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
	private defaultModels: string[] = [];
	private onSlashCommand?: TuiOptions["onSlashCommand"];
	private showThinking: boolean;
	private contextWindow: number;

	private history: TuiHistoryEntry[] = [];
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
		this.defaultModels = options.defaultModels ?? [];
		this.onSlashCommand = options.onSlashCommand;
		this.showThinking = options.showThinking ?? false;
		this.contextWindow = options.contextWindow ?? 0;
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
		for (const line of buildWelcomeBox(cols, this.provider, this.model)) {
			this.appendHistory({ role: "system", text: line });
		}
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
					this.appendHistory({ role: "assistant", text: this.assistantText(event.message) });
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
					text: `● ${event.toolName}(${safeStringify(event.args)})`,
				});
				break;

			case "tool_execution_end": {
				// Update the last tool entry to show result status inline.
				let lastToolIdx = -1;
				for (let i = this.history.length - 1; i >= 0; i--) {
					if (this.history[i]!.role === "tool") { lastToolIdx = i; break; }
				}
				if (lastToolIdx >= 0) {
					const prev = this.history[lastToolIdx]!;
					const icon = event.isError ? `${RED}✗${RESET}` : `${GREEN}✓${RESET}`;
					this.history[lastToolIdx] = {
						role: "tool",
						text: `${icon} ${prev.text.replace(/^● /, "")}`,
					};
				} else {
					this.appendHistory({
						role: "tool",
						text: `${event.isError ? "✗" : "✓"} ${event.toolName}`,
					});
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
	private scheduleRender(delay = 32): void {
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
	appendSystemLine(text: string): void {
		this.appendHistory({ role: "system", text });
		this.render();
	}

	/** Clear the in-memory chat history. */
	clearHistory(): void {
		this.history = [];
		this.render();
		this.onClear?.();
	}

	/** Read a setting by key. The TUI doesn't itself own a settings store;
	 *  hosts that want /settings to actually do something can supply a
	 *  callback. For now we return undefined for unknown keys. */
	getSetting(_key: string): unknown {
		return undefined;
	}

	/** Write a setting by key. Same caveat as `getSetting`. */
	setSetting(_key: string, _value: unknown): void {
		// No-op by default; hosts can subclass or wrap to wire this up.
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

	private handleInput(chunk: Buffer): void {
		const s = chunk.toString();
		if (this.mode === "selector") {
			this.handleSelectorInput(s);
			return;
		}
		if (s === "\x03" || s === "\x1b") {
			this.quit();
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
		if (s === "\x7f" || s === "\b") {
			this.inputBuffer = this.inputBuffer.slice(0, -1);
			this.updateSuggestions();
			this.render();
			return;
		}
		if (s === "\x0c") {
			this.repaint();
			return;
		}
		if (s.startsWith("\x1b[")) {
			if (s === "\x1b[A" || s === "\x1b[B") {
				// When the input buffer is non-empty OR we're already browsing
				// history, ↑/↓ walks through previous prompts. Otherwise
				// they scroll the chat backbuffer.
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
							this.render();
							return;
						}
					}
					this.inputBuffer = this.inputHistory[this.inputHistoryCursor] ?? "";
					this.render();
				} else {
					this.scroll(s === "\x1b[A" ? -1 : 1);
				}
				return;
			}
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
					this.inputBuffer += this.pasteBuffer.replace(/\n$/, "");
					this.pasteBuffer = "";
				}
			}
			this.inputBuffer += s;
			this.updateSuggestions();
			this.render();
		}
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
		}
	}

	/** Tab-complete the current slash-command input. If there's a unique
	 * common prefix or a single match, fill in the command name. */
	private completeSlashCommand(): void {
		if (this.suggestions.length === 0) return;
		if (this.suggestions.length === 1) {
			const name = this.suggestions[0]!.name;
			this.inputBuffer = name;
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
				} else {
					this.render();
				}
				return;
			}
		}

		this.busy = true;
		this.status = "thinking…";
		this.busyStartTime = Date.now();
		this.startElapsedTimer();
		this.render();

		try {
			await this.onSubmit(text);
		} catch (err) {
			this.appendHistory({
				role: "system",
				text: `[error] ${err instanceof Error ? err.message : String(err)}`,
			});
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
			? Math.min(this.suggestions.length, 6) // status + input + suggestions
			: 0;
		const reserved = 2 + sugCount; // status line + input line (+ suggestions)
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

		// Input line: prefix + buffer.
		const inputLine = truncateToWidth(`${USER_PREFIX}${this.inputBuffer}${RESET}`, safeCols);
		const suggestionLines = this.renderSuggestions(safeCols).map((l) => truncateToWidth(l, safeCols));
		const frame = [...visible, statusLine, inputLine, ...suggestionLines].slice(0, rows);

		this.writeFrame(frame, safeCols, rows);
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

	/** Render the slash-command suggestion popup beneath the input line. */
	private renderSuggestions(cols: number): string[] {
		if (!this.showSuggestions || this.suggestions.length === 0) return [];
		const max = 6; // cap visible height
		const items = this.suggestions.slice(0, max);
		const nameWidth = Math.min(
			cols,
			Math.max(...items.map((s) => s.name.length)),
		);
		return items.map((s, i) => {
			const dim = i === 0 ? YELLOW : DIM;
			const desc = s.description ?? "";
			return `${dim}${s.name.padEnd(nameWidth)} ${RESET}${desc.slice(0, Math.max(0, cols - nameWidth - 1))}`;
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
