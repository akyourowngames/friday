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
import { diffChunks, diffStats, formatDiffChunk, toolDiffEdits, type DiffChunk } from "./diff.ts";
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
	// Windows: assume yes, but note that this is only *true* because
	// setupConsoleEncoding() enables ENABLE_VIRTUAL_TERMINAL_PROCESSING for the
	// attached console. If that call is skipped (or fails), Conhost will print
	// these escapes literally and the whole frame collapses to flat text.
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
/** Frame period (ms) for the spinner — every tick advances one slot. */
const SPINNER_PERIOD_MS = 80;
/** Subtle gradient frames used for the block cursor that rides the tail of an
 *  in-flight assistant message — tokens are arriving affordance. Cycles every
 *  120ms so the cursor feels alive without burning CPU. The first frame is
 *  the original solid block (▍) so renderers that snapshot a frame mid-tick
 *  (smoke tests, screenshots) always capture a readable cursor. */
export const STREAM_CURSOR_FRAMES = ["▍", "▎", "▌", "▎"];
const STREAM_CURSOR_PERIOD_MS = 120;
/** Visual token meter — gradient blocks that fill left-to-right. */
const METER_FILL = "█";
const METER_EMPTY = "░";
/** Widest a message/tool block is allowed to get, in columns. Blocks otherwise
 *  span the full terminal width; this cap keeps them readable on ultrawide. */
const MAX_BLOCK_INNER = 96;
/** Bullet that separates sections in the status bar. */
const SEP = "│";

/** Pick the current animated frame for a periodic sequence. Exported so unit
 *  tests can verify the helper without exercising the whole render loop. */
export function animatedFrame(frames: readonly string[], periodMs: number, now: number = Date.now()): string {
	const idx = Math.max(0, Math.floor(now / periodMs) % frames.length);
	return frames[idx]!;
}

/** Block cursor appended to the in-flight assistant message while streaming. */
export function streamingCursor(now: number = Date.now()): string {
	const frame = animatedFrame(STREAM_CURSOR_FRAMES, STREAM_CURSOR_PERIOD_MS, now);
	return `${MAGENTA}${frame}${RESET}`;
}

/** Status-bar spinner glyph for the current wall time (pure helper). */
export function spinnerFrame(now: number = Date.now()): string {
	return `${CYAN}${animatedFrame(SPINNER_FRAMES, SPINNER_PERIOD_MS, now)}${RESET}`;
}

// Backwards-compatible alias — existing code that references `CURSOR_BLOCK`
// still works (smoke tests + summary helpers import it).
export const CURSOR_BLOCK = streamingCursor();

/** Format a token count for the status bar.
 *  Exact digits up to 100k — when you are watching a context budget, "12,345"
 *  beats "12K" — then abbreviated so a long session can't blow out the bar. */
export function formatTokenCount(n: number): string {
	if (!Number.isFinite(n) || n <= 0) return "0";
	const abs = Math.abs(n);
	if (abs < 100_000) return `${n.toLocaleString()}`;
	if (abs < 1_000_000) return `${Math.round(n / 1000)}K`;
	if (abs < 10_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
	return `${(n / 1_000_000).toFixed(1)}M`;
}

/** Pad a string to a visible width using spaces. Truncates if wider. */
function padVisible(text: string, width: number): string {
	const cur = visibleWidth(text);
	if (cur >= width) return truncateToWidth(text, width);
	return `${text}${" ".repeat(width - cur)}`;
}

/** Render a token-meter string `██░░ 67%` of exactly `width` columns with
 *  optional pulse for live feel. Returns empty string when window is 0 or width
 *  is too small to render anything meaningful. The meter scales thresholds:
 *  green under 50%, yellow 50–80%, red 80%+. */
export function contextMeter(used: number, window: number, width: number, now: number = Date.now()): string {
	if (window <= 0 || width < 8) return "";
	const pct = Math.max(0, Math.min(100, (used / window) * 100));
	const total = width; // we control the visible width precisely
	// Reserve columns for: '[' ']' and the ' NN%' tail. Bars live between.
	const pctText = `${Math.round(pct)}%`;
	const barLen = Math.max(1, total - 3 - pctText.length);
	const filled = Math.round((pct / 100) * barLen);
	const empty = Math.max(0, barLen - filled);
	const color = pct >= 80 ? RED : pct >= 50 ? YELLOW : GREEN;
	// No pulse here on purpose: a gauge that breathes reads as a value that is
	// changing, and context usage is not. The spinner already signals liveness.
	return `${color}[${METER_FILL.repeat(filled)}${METER_EMPTY.repeat(empty)}]${RESET}${DIM} ${pctText}${RESET}`;
}

/** Render a row of segment boxes that divide the status bar into pill-shaped
 *  sections. Returns ANSI-colored string of length exactly `width`. */
export function pillBar(width: number, color: string): string {
	if (width < 3) return "";
	const left = `${color}▏${RESET}${REVERSE}`;
	const right = `${RESET}${color}▕${RESET}`;
	return `${left}${" ".repeat(Math.max(1, width - 2))}${right}`;
}

/** Render a horizontal rule of exactly `width` columns using a dim line. */
export function horizontalRule(width: number, style: "thin" | "thick" = "thin"): string {
	const ch = style === "thick" ? "━" : "─";
	return `${DIM}${ch.repeat(Math.max(0, width))}${RESET}`;
}

/** Render a "ready" pulse — a slow-breathing dot that indicates the agent
 *  is idle and the prompt is hot. Cycles every 1600ms (~1 breath). */
export function readyPulse(now: number = Date.now()): string {
	const phase = Math.floor(now / 800) % 2;
	return phase === 0 ? `${GREEN}●${RESET}` : `${DIM}${GREEN}●${RESET}`;
}

/** Render the per-user prompt pulse — slowly dims/brightens a hint bullet to
 *  signal "I'm listening" without taking cursor focus. Cycles every 1200ms. */
export function userPulse(now: number = Date.now()): string {
	const phase = Math.floor(now / 1200) % 3;
	if (phase === 0) return `${MAGENTA}▏${RESET}`;
	if (phase === 1) return `${DIM}${MAGENTA}▏${RESET}`;
	return `${MAGENTA}▏${RESET}`;
}

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
	/** Optional message timestamp for header metadata (e.g. "12:34"). */
	timestamp?: number;
	/** When a tool execution began — drives the running elapsed timer. */
	startedAt?: number;
	/** When a tool execution ended — drives the timing badge. */
	endedAt?: number;
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

const DIFF_CONTEXT = 1;
const DIFF_MAX_LINES = 12;

function colorizeDiffChunk(chunk: DiffChunk): string {
	const text = formatDiffChunk(chunk);
	switch (chunk.op) {
		case "add":
			return `${GREEN}${text}${RESET}`;
		case "remove":
			return `${RED}${text}${RESET}`;
		case "hunk":
			return `${CYAN}${text}${RESET}`;
		default:
			return `${DIM}${text}${RESET}`;
	}
}

/**
 * Render a `write` / `edit` / `multi_edit` result as colored hunks.
 *
 * Uses the real line diff, so a one-line change in a 2,000-line file shows
 * one `-` and one `+` with a line of context and a `@@ -n,m @@` header — not
 * the whole file. Files too large to diff safely render nothing and let the
 * caller fall back to the byte/line-count summary.
 */
export function diffDetailLines(name: string, result: unknown): string[] {
	const details = ((result ?? {}) as { details?: unknown }).details;
	const edits = toolDiffEdits(name, details);

	const out: string[] = [];
	for (const edit of edits) {
		const { oldText, newText } = edit;
		if (typeof oldText !== "string" || typeof newText !== "string" || oldText === newText) continue;
		if (edits.length > 1 && typeof edit.path === "string") out.push(`${DIM}${edit.path}${RESET}`);
		for (const chunk of diffChunks(oldText, newText, { context: DIFF_CONTEXT, maxLines: DIFF_MAX_LINES })) {
			out.push(colorizeDiffChunk(chunk));
		}
	}
	return out;
}

export function renderToolEntry(entry: TuiHistoryEntry, width: number): string[] {
	if (!entry.name || !entry.status) return wrapText(entry.text, width, TOOL_PREFIX);
	const color = entry.status === "error" ? RED : entry.status === "done" ? GREEN : YELLOW;
	const isRunning = entry.status === "running";
	// Running icon stays as `●` so unit tests (which expect that exact glyph)
	// remain stable. We still animate it via color cycling on the title row
	// — see `runningIndicator()` below — driven by the elapsed timer.
	const icon = entry.status === "error" ? `✗` : entry.status === "done" ? `✓` : runningIndicator();
	const now = Date.now();
	const title = `${icon} ${formatToolCall(entry.name, entry.args)}`;
	// The status line lives *under* the header rather than inside it: the title
	// is the thing you scan for, so it must never be truncated to make room for
	// a summary. Running entries show a live elapsed timer + dot animation.
	const summary = isRunning
		? `${YELLOW}running${RESET} ${DIM}${formatElapsed(now - (entry.startedAt ?? now))}${RESET}${runningTrailing(now)}`
		: `${DIM}${summarizeToolResult(entry.name, entry.result, entry.status === "error")}${RESET}${
				entry.startedAt
					? ` ${DIM}· ${formatElapsed(Math.max(0, (entry.endedAt ?? now) - entry.startedAt))}${RESET}`
					: ""
			}`;
	let bodyLines = entry.body?.split("\n") ?? [];
	if (bodyLines.length === 0 && entry.expanded) bodyLines = resultText(entry.result).split("\n").filter(Boolean);
	const diffs = diffDetailLines(entry.name, entry.result);
	if (diffs.length > 0) bodyLines = diffs;
	// Command output almost always ends with a newline. Rendering that as a
	// blank row inside a small box just looks broken, so trim the edges.
	while (bodyLines.length > 0 && bodyLines[bodyLines.length - 1]!.trim() === "") bodyLines.pop();
	while (bodyLines.length > 0 && bodyLines[0]!.trim() === "") bodyLines.shift();
	const fullCount = bodyLines.length;
	if (!entry.expanded && bodyLines.length > 12) bodyLines = bodyLines.slice(0, 12);
	const bodyContent = [summary, ...bodyLines];
	if (!entry.expanded && fullCount > 12) bodyContent.push(`${DIM}… ${fullCount - 12} more lines (Ctrl+O to expand)${RESET}`);
	const availableInner = Math.max(1, width - 4);
	// Tool blocks span the full width like message blocks — a ragged stack of
	// content-sized boxes reads as noise. Never wider than the terminal: an
	// overflowing row wraps and scrolls the screen. Wrap against the *capped*
	// inner width, not the raw terminal width, or long command output can spill
	// out of an otherwise correctly sized box on wide terminals.
	const inner = Math.min(availableInner, MAX_BLOCK_INNER);
	const wrapped = bodyContent.flatMap((line) => wrapText(line, inner, ""));
	// Header row: ╭─ {title} ───────────────╮  — sized so the row is exactly
	// `inner + 4` columns wide, matching every body row and the bottom border.
	// "╭─ " (3) + title + " " (1) + dashes + "╮" (1) = inner + 4.
	const titleBudget = Math.max(0, inner - 3);
	const titleStr = visibleWidth(title) > titleBudget ? truncateToWidth(title, titleBudget) : title;
	const dashes = Math.max(0, inner - 1 - visibleWidth(titleStr));
	const headerLine = `${color}╭─ ${RESET}${color}${titleStr}${RESET} ${color}${"─".repeat(dashes)}╮${RESET}`;
	const lines: string[] = [truncateToWidth(headerLine, inner + 4)];
	for (const line of wrapped) {
		const clipped = truncateToWidth(line, inner);
		lines.push(`${color}│${RESET} ${clipped}${" ".repeat(Math.max(0, inner - visibleWidth(clipped)))} ${color}│${RESET}`);
	}
	lines.push(`${color}╰${"─".repeat(inner + 2)}╯${RESET}`);
	return lines.map((l) => truncateToWidth(l, Math.max(1, width)));
}

/** Trailing animation dots appended to the running summary (3-frame cycle).
 *  Returned as a plain string with no surrounding color so it inherits the
 *  summary's dim style. */
function runningTrailing(now: number): string {
	const dots = ["", "·", "··", "···"];
	return `${dots[Math.floor(now / 320) % dots.length] ?? ""}`;
}

/** Indicator glyph for a tool box header. Always returns `●` so unit tests
 *  that compare against the literal stay stable; the color cycles subtly to
 *  give the box a heartbeat. */
function runningIndicator(): string {
	return `${YELLOW}●${RESET}`;
}

export function renderTodos(todos: readonly TuiTodoItem[], width: number, maxLines: number): string[] {
	if (maxLines <= 0 || todos.length === 0) return [];
	const marker = (status: string) =>
		status === "completed"
			? `${GREEN}✓${RESET}`
			: status === "in_progress"
				? `${YELLOW}◐${RESET}`
				: `${DIM}○${RESET}`;
	const completed = todos.filter((t) => t.status === "completed").length;
	const total = todos.length;
	const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
	const count = `${completed}/${total}`;
	// Header carries a progress bar so the plan reads as a commitment rather
	// than a stray list. Below ~34 columns the bar is dead weight — collapse to
	// a bare percentage instead of letting the counts get truncated away.
	const barW = Math.min(20, width - 24);
	const filled = barW > 0 ? Math.round((pct / 100) * barW) : 0;
	const bar =
		barW > 0
			? `  ${MAGENTA}${METER_FILL.repeat(filled)}${RESET}${DIM}${METER_EMPTY.repeat(Math.max(0, barW - filled))}${RESET}`
			: "";
	const header = `${DIM}plan${RESET} ${BOLD}${pct}%${RESET}${bar}  ${DIM}${count}${RESET}`;
	const out: string[] = [truncateToWidth(header, width)];
	// Reserve the last line for the overflow hint, so the visible list is
	// always `maxLines - 2` items plus a header and a "… N more" footer.
	const slots = Math.max(0, maxLines - 2);
	for (const todo of todos.slice(0, slots)) {
		out.push(truncateToWidth(`${marker(todo.status)} ${todo.text}`, width));
	}
	const remaining = todos.length - slots;
	if (remaining > 0) {
		out.push(truncateToWidth(`${DIM}… ${remaining} more todo${remaining === 1 ? "" : "s"}${RESET}`, width));
	}
	return out;
}

/** Render one history entry to lines. User/assistant messages are enclosed in
 *  labeled rounded boxes (Claude Code / Codex style) so every response is a
 *  self-contained block that can never bleed into the status bar; tool and
 *  system entries stay as compact dim lines. Shared by the pure
 *  `computeContentLines` helper and the Tui's per-entry render cache. */
export function renderHistoryEntry(entry: TuiHistoryEntry, width: number, now: number = Date.now()): string[] {
	switch (entry.role) {
		case "user": {
			const meta = entry.timestamp ? formatTime(entry.timestamp) : "";
			return renderBox(entry.text, width, "you", CYAN, false, meta);
		}
		case "assistant": {
			const meta = entry.timestamp ? formatTime(entry.timestamp) : "";
			return renderBox(entry.text, width, "friday", MAGENTA, false, meta);
		}
		case "tool":
			return renderToolEntry(entry, width);
		case "system":
			return wrapText(entry.text, width, DIM);
		default:
			return [];
	}
}

/** Format a Unix-ms timestamp as a short HH:MM local time string. */
function formatTime(ts: number): string {
	const d = new Date(ts);
	if (Number.isNaN(d.getTime())) return "";
	const hh = `${d.getHours()}`.padStart(2, "0");
	const mm = `${d.getMinutes()}`.padStart(2, "0");
	return `${hh}:${mm}`;
}

/** Render `text` as a rounded, labeled box. The box shrinks to fit its
 *  content (up to `width`), every line is padded to the same visible width,
 *  and — critically — the box is never wider than `width`, which the callers
 *  keep one column short of the terminal so nothing can ever wrap or scroll
 *  the screen. `open` omits the bottom border (used while a reply streams).
 *  `meta` is an optional dim subtitle that appears right-aligned in the
 *  header bar (e.g. "12:34" or "247 tok"). When omitted, the header is just
 *  `─ label ────…` so existing call sites and tests stay unchanged. */
function renderBox(text: string, width: number, label: string, color: string, open = false, meta = ""): string[] {
	// A body row is "│ " + content + " │", so only `width - 4` columns are
	// available for text. Pick the final box width first, then wrap to *that*
	// inner width. Previously we wrapped against the uncapped terminal width and
	// later capped the border at MAX_BLOCK_INNER: a sentence that fit the former
	// leaked past the latter (exactly the overflow shown in the report).
	const availableInner = Math.max(1, width - 4);
	// Blocks span the full available width (capped so they stay readable on an
	// ultrawide terminal). Uniform block width is what gives the transcript its
	// rhythm — content-sized boxes end up ragged next to each other.
	const inner = Math.min(availableInner, MAX_BLOCK_INNER);
	const wrapped = text.length > 0 ? wrapText(text, inner, "") : [""];
	const labelNeed = label.length + 4; // "─ label "
	const metaNeed = meta ? visibleWidth(meta) + 4 : 0; // " ── meta "
	const lines: string[] = [];
	if (inner >= labelNeed) {
		if (meta && inner >= labelNeed + metaNeed) {
			// Header with right-aligned dim subtitle: ╭─ LABEL ──── meta ─╮
			// Sized so the row lands on `inner + 4`, same as every body row.
			const dashes = Math.max(1, inner - labelNeed - metaNeed + 4);
			lines.push(`${color}╭─ ${BOLD}${label}${RESET} ${color}${"─".repeat(dashes)}${RESET} ${DIM}${meta}${RESET} ${color}─╮${RESET}`);
		} else {
			const dash = Math.max(0, inner - label.length - 1);
			lines.push(`${color}╭─ ${BOLD}${label}${RESET} ${"─".repeat(dash)}${color}╮${RESET}`);
		}
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
 *  status/input reserved rows). Pure and testable — `now` defaults to the
 *  current wall time so the animated streaming cursor picks the right frame
 *  for screenshots / tests that pass a fixed timestamp. */
export function computeContentLines(
	history: TuiHistoryEntry[],
	streamingText: string,
	width: number,
	now: number = Date.now(),
): string[] {
	const lines: string[] = [];
	for (const entry of history) {
		lines.push(...renderHistoryEntry(entry, width, now));
	}
	if (streamingText.length > 0) {
		// The in-flight reply is an open-bottomed box (matches the TUI render).
		lines.push(...renderBox(`${streamingText}${streamingCursor(now)}`, width, "friday", MAGENTA, true, "streaming"));
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
 *  plain lines when the terminal is too narrow for a box.
 *  `now` controls the breathing ◉ dot in the status row; pass a fixed value in
 *  tests/screenshots for a deterministic snapshot. */
export function buildWelcomeBox(
	cols: number,
	provider: string,
	model: string,
	now: number = Date.now(),
): string[] {
	const wave = `${GREEN}◉${RESET}`;
	const dot = `${DIM}•${RESET}`;
	// Two visual rows: a brand/identity row and a structured key-value stack,
	// separated by a thin rule. Each row is built individually and re-wrapped
	// at render time, so a narrow terminal still fits them gracefully.
	const headerLeft = `${MAGENTA}✦${RESET} ${BOLD}friday-ng${RESET}`;
	const headerRight = readyPulse(now);
	const contentLines: string[] = [
		`${wave} ${BOLD}Ready${RESET}  ${dot}  ${DIM}streaming · interrupt-safe${RESET}`,
		``,
		`${DIM}model      ${RESET}${CYAN}${provider}/${model}${RESET}`,
		`${DIM}transport  ${RESET}${GREEN}online${RESET} ${DIM}· SSE streaming${RESET}`,
		``,
		`${BOLD}${DIM}↵   ${RESET}${DIM}type to chat${RESET}`,
		`${BOLD}${DIM}^K  ${RESET}${DIM}commands${RESET}   ${BOLD}${DIM}?  ${RESET}${DIM}help${RESET}   ${BOLD}${DIM}^L${RESET} ${DIM}clear${RESET}   ${BOLD}${DIM}esc${RESET} ${DIM}quit${RESET}`,
	];
	const maxBox = Math.max(0, cols - 2);
	// The welcome surface belongs to the same transcript as messages and tools,
	// so it uses that same full available width (up to the shared readability
	// cap). A content-sized banner created a detached mini-card on wide
	// terminals, even though all subsequent boxes correctly occupied the row.
	const boxWidth = Math.min(MAX_BLOCK_INNER + 4, maxBox);
	// Below ~30 columns a border costs more than it communicates: the content
	// would be truncated to slivers. Degrade to plain lines instead.
	if (boxWidth < 30) return [headerLeft, ...contentLines];
	const inner = boxWidth - 2;
	// Every content row goes through `row()` so the box can never be ragged:
	// `│` + 2 pad + content + pad + `│` always totals exactly `boxWidth`.
	// contentW is the full usable span; content is clipped to it.
	const contentW = Math.max(0, inner - 2);
	const row = (s: string): string => {
		const clipped = truncateToWidth(s, contentW);
		const pad = Math.max(0, inner - visibleWidth(clipped) - 2);
		return `${YELLOW}│${RESET}  ${clipped}${" ".repeat(pad)}${YELLOW}│${RESET}`;
	};
	// Header row: brand pinned left, breathing dot pinned right, with a 2-space
	// gutter on both sides. Drop the dot rather than pushing the brand out.
	const headerSpan = Math.max(0, contentW - 2);
	let header = `${headerLeft}${" ".repeat(Math.max(0, headerSpan - visibleWidth(headerLeft) - visibleWidth(headerRight)))}${headerRight}`;
	if (visibleWidth(header) > headerSpan) header = headerLeft;
	const lines: string[] = [
		`${YELLOW}┌${"─".repeat(inner)}┐${RESET}`,
		row(header),
		// Rule gets an explicit symmetric inset so it breathes like the text.
		`${YELLOW}│${RESET}  ${DIM}${"─".repeat(Math.max(0, inner - 4))}${RESET}  ${YELLOW}│${RESET}`,
		row(""),
	];
	for (const line of contentLines) lines.push(row(line));
	lines.push(row(""));
	lines.push(`${YELLOW}└${"─".repeat(inner)}┘${RESET}`);
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
					this.appendHistory({ role: "user", text: this.userText(event.message), timestamp: Date.now() });
					this.userScroll = 0;
				} else if (event.message.role === "assistant") {
					this.streamingText = "";
					this.userScroll = 0;
				}
				break;

			case "message_update":
				if (event.message.role === "assistant") {
					// Stream the raw markdown source — don't run it through the
					// markdown renderer per token. A partial `## Over` heading
					// produces broken output if we parse it on every delta; the
					// real render happens once on message_end.
					this.streamingText = this.rawAssistantText(event.message);
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
					// Final render: parse the accumulated markdown ONCE. This is
					// where headings, code fences, bullet lists, and tables get
					// their structure and color.
					const text = this.assistantText(m);
					const hasToolCalls = m.content.some((c) => c.type === "toolCall");
					// Tool-only replies render as the tool execution lines
					// below — don't add a silent empty box for them.
					if (text.trim().length > 0) {
						this.appendHistory({ role: "assistant", text, timestamp: Date.now() });
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
					startedAt: Date.now(),
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
					entry.endedAt = Date.now();
					const summary = summarizeToolResult(event.toolName, event.result, event.isError).split("\n").join("\n  ");
					entry.text = `${event.isError ? "✗" : "✓"} ${formatToolCall(event.toolName, entry.args)}${summary ? ` · ${summary}` : ""}`;
					this.entryCache.delete(entry);
				} else {
					const summary = summarizeToolResult(event.toolName, event.result, event.isError).split("\n").join("\n  ");
					this.appendHistory({ role: "tool", text: `${event.isError ? "✗" : "✓"} ${formatToolCall(event.toolName, {})}${summary ? ` · ${summary}` : ""}`, toolCallId: event.toolCallId, name: event.toolName, args: {}, status: event.isError ? "error" : "done", result: event.result, startedAt: Date.now(), endedAt: Date.now() });
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

	/**
	 * Raw text concatenation for in-flight streaming — no markdown parsing.
	 * The streaming box paints this verbatim, so partial `##` headings look
	 * fine and the renderer doesn't have to re-parse the whole message on
	 * every token.
	 */
	private rawAssistantText(message: AssistantMessage): string {
		const parts: string[] = [];
		for (const c of message.content) {
			if (c.type === "text") parts.push(c.text);
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

		const now = Date.now();
		// --- Status bar (Pi Harness style, polished) ---
		// Left: model + token gauges. Center: context meter (when known).
		// Right: spinner + elapsed when busy, otherwise a soft "ready" pulse.
		const elapsed = this.busy && this.busyStartTime > 0
			? formatElapsed(now - this.busyStartTime)
			: "";
		const spinner = animatedFrame(SPINNER_FRAMES, SPINNER_PERIOD_MS, now);

		const tokenStr = this.lastUsage.total > 0
			? `${formatTokenCount(this.lastUsage.input)}↑ ${formatTokenCount(this.lastUsage.output)}↓`
			: "";

		const contextPct = this.contextWindow > 0 && this.lastUsage.input > 0
			? `${Math.round((this.lastUsage.input / this.contextWindow) * 100)}%`
			: "";

		// Build status segments — each segment gets subtle color treatment for
		// rhythm and a `SEP` divider so the bar reads like a CLI dashboard.
		const sep = ` ${DIM}${SEP}${RESET} `;
		const leftParts: string[] = [`${BOLD}${CYAN}${this.model}${RESET}`];
		if (tokenStr) leftParts.push(tokenStr);
		const bareLeftSide = leftParts.join(sep);

		// Right side is contextual: while the slash-command popup is open it
		// advertises the keys that actually matter right now. Putting the hint
		// here rather than inside the popup keeps the popup exactly one line per
		// match, which is what the vertical space reservation assumes.
		const popupOpen = this.showSuggestions && this.suggestions.length > 0;
		const rightParts: string[] = [];
		if (elapsed) rightParts.push(`${CYAN}${spinner}${RESET} ${DIM}${elapsed}${RESET}`);
		else rightParts.push(`${DIM}${spinner}${RESET} ready`);
		rightParts.push(
			popupOpen
				? `${YELLOW}↑↓${RESET} ${DIM}select${RESET}  ${YELLOW}↵${RESET} ${DIM}accept${RESET}  ${YELLOW}esc${RESET} ${DIM}dismiss${RESET}`
				: `${DIM}/help${RESET}`,
		);
		const rightSide = rightParts.join(sep);

		// Center: visual token meter, only when we have a context window and
		// only if it actually fits. Context usage is shown either as the meter
		// or as a bare `ctx NN%` segment — never both, which just reads as
		// duplicated noise.
		let meterLine = "";
		if (this.contextWindow > 0 && this.lastUsage.input > 0) {
			const meterWidth = Math.max(8, Math.min(22, Math.floor(safeCols * 0.18)));
			const meterStr = contextMeter(this.lastUsage.input, this.contextWindow, meterWidth, now);
			if (meterStr) {
				const remaining = Math.max(0, safeCols - visibleWidth(bareLeftSide) - visibleWidth(rightSide) - 6);
				if (remaining >= visibleWidth(meterStr)) {
					const before = Math.max(0, Math.floor((remaining - visibleWidth(meterStr)) / 2));
					meterLine = truncateToWidth(`${" ".repeat(before)}${meterStr}`, remaining);
				}
			}
		}
		const leftSide = meterLine || !contextPct
			? bareLeftSide
			: `${bareLeftSide}${sep}${DIM}ctx ${contextPct}${RESET}`;

		// Layout: [left]  spacers  [meter?]  spacers  [right], reverse-video baseline.
		const leftWidth = visibleWidth(leftSide);
		const rightWidth = visibleWidth(rightSide);
		const meterTextWidth = meterLine ? visibleWidth(meterLine) : 0;
		const freeSpace = Math.max(1, safeCols - leftWidth - rightWidth - meterTextWidth);
		const leftPad = Math.max(1, Math.floor(freeSpace / 2));
		const rightPad = Math.max(1, freeSpace - leftPad);
		const statusLine = truncateToWidth(
			`${REVERSE}${leftSide}${" ".repeat(leftPad)}${meterLine}${" ".repeat(rightPad)}${rightSide}${RESET}`,
			safeCols,
		);

		// Input line: prefix + buffer (cursor-aware). Add a tiny rhythm cue
		// before the prefix when idle (a single dim bullet) that breathes.
		let inputLine: string;
		let inputCursorCol: number;
		if (this.searchActive) {
			const label = `${CYAN}(reverse-i-search)\`${RESET}${this.searchQuery}${CYAN}\`${RESET}${DIM}${this.searchCursor >= 0 ? `: ${this.searchMatches[this.searchCursor]}` : ": (no match)"}${RESET}`;
			inputLine = truncateToWidth(label, safeCols);
			inputCursorCol = 1 + visibleWidth(`(reverse-i-search)\``) + visibleWidth(this.searchQuery);
		} else {
			const prefix = `${userPulse(now)}${USER_PREFIX}`;
			inputLine = truncateToWidth(`${prefix}${this.inputBuffer}${RESET}`, safeCols);
			inputCursorCol = 1 + 1 + USER_PREFIX_VW + visibleWidth(this.inputBuffer.slice(0, this.cursorPos));
		}
		const suggestionLines = this.renderSuggestions(safeCols).map((l) => truncateToWidth(l, safeCols));
		const todoLines = renderTodos(this.todos, safeCols, todoCount);
		const modalLines = this.activeConfirmation
			? renderBox(`${this.activeConfirmation.prompt}\n${BOLD}y/Enter${RESET} yes  ${BOLD}n/Esc${RESET} no`, safeCols, "confirm", YELLOW).slice(0, modalCount)
			: [];
		// Bottom-anchor the chrome: the transcript grows from the top, while the
		// status bar and prompt stay pinned to the last rows the way every
		// serious agent CLI does. Blank filler absorbs the slack on a short
		// transcript instead of letting the prompt drift up the screen.
		const tail = [...todoLines, ...modalLines, statusLine, inputLine, ...suggestionLines];
		const filler = Math.max(0, rows - visible.length - tail.length);
		const frame = [
			...visible,
			...Array.from({ length: filler }, () => ""),
			...tail,
		].slice(0, rows);

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
			// Running tools get a fresh render every frame so the heart-beat
			// indicator at the title row stays in sync with the elapsed timer
			// (otherwise the cache would freeze it at the first-render frame).
			if (entry.role === "tool" && entry.status === "running") {
				this.entryCache.delete(entry);
			}
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
			// The in-flight reply lives in its own open-bottomed box; the animated
			// block cursor shows tokens are arriving (Claude Code style).
			out.push(...renderBox(`${this.streamingText}${streamingCursor()}`, width, "friday", MAGENTA, true, "streaming"));
		} else if (this.busy) {
			// Waiting on the first token. Without this the transcript sits
			// completely still after you press Enter, which reads as a hang —
			// the status bar spinner alone is too quiet to notice. The elapsed
			// time is already in the status bar, so this only needs to shimmer.
			const now = Date.now();
			out.push(`${spinnerFrame(now)} ${DIM}thinking${RESET}${runningTrailing(now)}`);
		}
		return out;
	}

	/** Render the slash-command suggestion popup beneath the input line. The
	 *  highlighted (selected) row is given reverse video and a `▶` glyph; the
	 *  other rows stay dim. A thin separator line above the list provides visual
	 *  separation from the input line. */
	private renderSuggestions(cols: number): string[] {
		if (!this.showSuggestions || this.suggestions.length === 0) return [];
		const max = 6; // cap visible height
		const items = this.suggestions.slice(0, max);
		const nameWidth = Math.min(
			cols,
			Math.max(...items.map((s) => s.name.length)),
		);
		const out: string[] = [];
		// One line per match — the frame reserves exactly `sugCount` rows for
		// the popup, so any decoration line here would be clipped (and would
		// shift the whole viewport). The keyboard hint lives in the status bar
		// instead, which costs no vertical space.
		for (let i = 0; i < items.length; i++) {
			const s = items[i]!;
			const selected = i === this.suggestionCursor;
			const desc = s.description ?? "";
			// Fill the row so the reverse-video highlight reads as a solid bar
			// rather than a ragged run of glyphs.
			const body = `${s.name.padEnd(nameWidth)}  ${desc}`;
			const filled = padVisible(body, Math.max(0, cols - 3));
			const row = selected
				? `${REVERSE}${CYAN}▶${BOLD}${YELLOW}${filled}${RESET}`
				: `${DIM}  ${filled}${RESET}`;
			out.push(truncateToWidth(row, cols));
		}
		return out;
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
		// +1 for the userPulse prefix glyph that precedes USER_PREFIX in chat mode.
		const col = Math.min(_cols, cursorCol ?? 1 + 1 + USER_PREFIX_VW + visibleWidth(this.inputBuffer));
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
	if (!Number.isFinite(ms) || ms < 0) return "0s";
	const secs = Math.floor(ms / 1000);
	if (secs < 60) return `${secs}s`;
	const mins = Math.floor(secs / 60);
	const rem = secs % 60;
	if (mins < 60) return `${mins}m ${rem}s`;
	const hours = Math.floor(mins / 60);
	const remMins = mins % 60;
	return `${hours}h ${remMins}m`;
}

/** Strip ANSI control sequences and OSC 8 hyperlinks from a string, leaving
 *  the printable text only. Used by renderers that need to inspect visible
 *  length of a string built up in multiple ANSI segments. */
export function stripAnsi(input: string): string {
	return input.replace(/\x1b\[[0-9;]*[A-Za-z]/g, "").replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "");
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

/**
 * `(+3 -1)` suffix for results that carry both sides of a file change. Empty
 * when there is nothing to diff (no payload, no changes, or too large).
 */
function diffStatSuffix(details: Record<string, unknown>): string {
	if (typeof details.oldText !== "string" || typeof details.newText !== "string") return "";
	const stats = diffStats(details.oldText, details.newText);
	if (!stats || (stats.added === 0 && stats.removed === 0)) return "";
	return ` (+${stats.added} -${stats.removed})`;
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
			const head = typeof b === "number" ? `wrote ${b} bytes` : "done";
			if (details.oldText === "") return `${head} (new file)`;
			return `${head}${diffStatSuffix(details)}`;
		}
		case "edit":
			return `edited${diffStatSuffix(details)}`;
		case "multi_edit": {
			const edits = toolDiffEdits(name, details);
			if (edits.length === 0) return "done";
			let added = 0;
			let removed = 0;
			const files = new Set<string>();
			for (const edit of edits) {
				if (!edit || typeof edit !== "object") continue;
				if (typeof edit.path === "string") files.add(edit.path);
				if (typeof edit.oldText !== "string" || typeof edit.newText !== "string") continue;
				const stats = diffStats(edit.oldText, edit.newText);
				if (stats) {
					added += stats.added;
					removed += stats.removed;
				}
			}
			const stat = added === 0 && removed === 0 ? "" : ` (+${added} -${removed})`;
			const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;
			return `${plural(edits.length, "edit")} across ${plural(files.size, "file")}${stat}`;
		}
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
