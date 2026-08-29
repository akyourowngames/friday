/**
 * Tiny Markdown renderer for the TUI.
 *
 * Pi's TUI uses a custom terminal-aware markdown renderer (`@earendil-works/pi-tui`).
 * We don't need a full CommonMark implementation here — the assistant's output
 * in chat is mostly plain prose, code blocks, inline code, headers, lists,
 * bold, italic, and links. This module produces a sequence of plain lines
 * (one per source line) that can be fed to the TUI's wrap function.
 *
 * The render output is intentionally simple: it strips the markdown
 * punctuation and keeps the text, so a line like `**bold**` becomes `bold`
 * with a `BOLD_OPEN`/`BOLD_CLOSE` token pair wrapped around the inner text
 * (or just inline). For our needs, returning the raw text with `code` and
 * `code-block` regions tagged is enough.
 */

export type MarkdownSpan =
	| { kind: "text"; text: string; bold?: boolean; italic?: boolean; code?: boolean }
	| { kind: "code-block"; lang: string; text: string };

export interface MarkdownLine {
	spans: MarkdownSpan[];
}

/** Parse a single line of markdown into a sequence of spans.
 *
 *  GUARANTEED PROGRESS: every branch advances `i`. Unclosed markers (`` ` ``,
 *  `**`, `*`) are emitted as literal text instead of spinning in place —
 *  mid-stream partial text hits this constantly, and the old fall-through
 *  (`nextSpecial` returning `i` itself) allocated spans forever, which is the
 *  4GB "JavaScript heap out of memory" crash from todo.txt.
 */
function parseLine(line: string): MarkdownSpan[] {
	const out: MarkdownSpan[] = [];
	let i = 0;
	while (i < line.length) {
		// Inline code: `…`
		if (line[i] === "`") {
			const end = line.indexOf("`", i + 1);
			if (end > i) {
				out.push({ kind: "text", text: line.slice(i + 1, end), code: true });
				i = end + 1;
				continue;
			}
			// Unclosed backtick — literal char, advance.
			out.push({ kind: "text", text: line.slice(i, i + 1) });
			i += 1;
			continue;
		}
		// Bold: **…**
		if (line.startsWith("**", i)) {
			const end = line.indexOf("**", i + 2);
			if (end > i) {
				out.push({ kind: "text", text: line.slice(i + 2, end), bold: true });
				i = end + 2;
				continue;
			}
			// Unclosed bold marker — literal char, advance.
			out.push({ kind: "text", text: line.slice(i, i + 1) });
			i += 1;
			continue;
		}
		// Italic: *…*
		if (line[i] === "*" && line[i + 1] !== "*") {
			const end = line.indexOf("*", i + 1);
			if (end > i) {
				out.push({ kind: "text", text: line.slice(i + 1, end), italic: true });
				i = end + 1;
				continue;
			}
			// Unclosed italic marker — literal char, advance.
			out.push({ kind: "text", text: line.slice(i, i + 1) });
			i += 1;
			continue;
		}
		// Plain run until the next interesting character.
		const next = nextSpecial(line, i);
		if (next <= i) {
			// Safety net: never iterate without progress.
			out.push({ kind: "text", text: line.slice(i, i + 1) });
			i += 1;
			continue;
		}
		out.push({ kind: "text", text: line.slice(i, next) });
		i = next;
	}
	// Drop empty plain spans.
	return out.filter((s) => !(s.kind === "text" && s.text === ""));
}

function nextSpecial(line: string, start: number): number {
	let i = start;
	while (i < line.length) {
		const c = line[i]!;
		if (c === "`" || c === "*") return i;
		i += 1;
	}
	return i;
}

/** Render a multi-line markdown string into a sequence of `MarkdownLine`s.
 *  Tracks fenced code blocks (` ``` ` … ` ``` `) as a single block. */
export function renderMarkdown(input: string): MarkdownLine[] {
	const lines = input.split("\n");
	const out: MarkdownLine[] = [];
	let i = 0;
	while (i < lines.length) {
		const line = lines[i]!;
		const fence = /^```([\w-]*)\s*$/.exec(line);
		if (fence) {
			const lang = fence[1] ?? "";
			const block: string[] = [];
			i += 1;
			while (i < lines.length && !/^```\s*$/.test(lines[i]!)) {
				block.push(lines[i]!);
				i += 1;
			}
			i += 1; // skip closing fence
			out.push({ spans: [{ kind: "code-block", lang, text: block.join("\n") }] });
			continue;
		}
		// Header: #, ##, ### at the start
		const header = /^(#{1,6})\s+(.+)$/.exec(line);
		if (header) {
			out.push({
				spans: [{ kind: "text", text: header[2]!, bold: true }],
			});
			i += 1;
			continue;
		}
		// Blockquote: > …
		const bq = /^>\s*(.*)$/.exec(line);
		if (bq) {
			out.push({ spans: [{ kind: "text", text: `│ ${bq[1] ?? ""}` }] });
			i += 1;
			continue;
		}
		// Unordered list: - foo or * foo
		const ul = /^[-*]\s+(.+)$/.exec(line);
		if (ul) {
			out.push({ spans: parseLine(`• ${ul[1]!}`) });
			i += 1;
			continue;
		}
		// Ordered list: 1. foo
		const ol = /^\d+\.\s+(.+)$/.exec(line);
		if (ol) {
			out.push({ spans: parseLine(ol[1]!) });
			i += 1;
			continue;
		}
		// Horizontal rule
		if (/^---+\s*$/.test(line) || /^\*\*\*+\s*$/.test(line)) {
			out.push({ spans: [{ kind: "text", text: "─".repeat(20) }] });
			i += 1;
			continue;
		}
		// Default: inline parse.
		out.push({ spans: parseLine(line) });
		i += 1;
	}
	return out;
}

/** Flatten a MarkdownLine into a plain-text string (drops formatting). */
export function markdownToPlain(input: string): string {
	const lines = renderMarkdown(input);
	const out: string[] = [];
	for (const line of lines) {
		let lineText = "";
		for (const s of line.spans) {
			if (s.kind === "code-block") {
				out.push(s.text);
				continue;
			}
			lineText += s.text;
		}
		if (lineText) out.push(lineText);
	}
	return out.join("\n");
}

const ESC = "\x1b";
const BOLD = `${ESC}[1m`;
const DIM = `${ESC}[2m`;
const FG_CYAN = `${ESC}[36m`;
const FG_GREEN = `${ESC}[32m`;
const FG_YELLOW = `${ESC}[33m`;
const FG_RED = `${ESC}[31m`;
const BG_DIM = `${ESC}[48;5;236m`;
const RESET = `${ESC}[0m`;

/** ANSI color names for code-block language highlighting. */
const LANG_COLORS: Record<string, string> = {
	ts: FG_CYAN,
	typescript: FG_CYAN,
	js: FG_GREEN,
	javascript: FG_GREEN,
	bash: FG_YELLOW,
	sh: FG_YELLOW,
	py: FG_GREEN,
	python: FG_GREEN,
	json: FG_CYAN,
	css: FG_RED,
	html: FG_RED,
	yaml: FG_GREEN,
	yml: FG_GREEN,
};

export interface ColoredLine {
	/** The line with ANSI escape codes embedded. */
	text: string;
}

export interface RenderColoredOptions {
	/** Width to wrap lines at. Default: no wrapping. */
	wrapWidth?: number;
}

/**
 * Render markdown to an array of ANSI-colored strings, suitable for a
 * color-capable terminal. Code blocks get a dim background, headers get bold,
 * inline code gets cyan, bold/italic are honored.
 */
export function renderMarkdownColored(input: string, opts: RenderColoredOptions = {}): string[] {
	const lines = renderMarkdown(input);
	const out: string[] = [];
	for (const line of lines) {
		if (line.spans.length === 1 && line.spans[0]?.kind === "code-block") {
			const span = line.spans[0]!;
			const lang = span.lang ? span.lang : "txt";
			const color = LANG_COLORS[lang] ?? FG_CYAN;
			const blockLines = span.text.split("\n");
			for (const bl of blockLines) {
				// Indent code lines; use a dim background for contrast.
				out.push(`${BG_DIM}  ${color}${bl}${RESET}`);
			}
			continue;
		}
		let text = "";
		for (const s of line.spans) {
			if (s.kind === "text") {
				let seg = s.text;
				if (s.bold) seg = `${BOLD}${seg}${RESET}`;
				if (s.italic) seg = `${DIM}${seg}${RESET}`;
				if (s.code) seg = `${FG_CYAN}${seg}${RESET}`;
				text += seg;
			}
		}
		if (opts.wrapWidth && text.length > opts.wrapWidth) {
			// Simple word-wrap for colored text (strips ANSI for width calc).
			out.push(...wrapColored(text, opts.wrapWidth));
		} else {
			out.push(text || "");
		}
	}
	return out;
}

/** Wrap a single ANSI-colored string to `width` (preserves color codes). */
function wrapColored(text: string, width: number): string[] {
	const out: string[] = [];
	let line = "";
	for (const word of text.split(/(\s+)/)) {
		// Strip ANSI to measure visible length.
		const visible = word.replace(/\x1b\[[0-9;]*m/g, "");
		if (line.replace(/\x1b\[[0-9;]*m/g, "").length + visible.length > width && line.replace(/\x1b\[[0-9;]*m/g, "").length > 0) {
			out.push(line.trimEnd());
			line = word;
		} else {
			line += word;
		}
	}
	out.push(line.trimEnd());
	return out;
}
