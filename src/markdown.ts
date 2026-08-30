export type TableAlignment = "left" | "center" | "right";

export type MarkdownSpan =
	| { kind: "text"; text: string; bold?: boolean; italic?: boolean; code?: boolean }
	| { kind: "code-block"; lang: string; text: string }
	| { kind: "table"; rows: string[][]; alignments: TableAlignment[]; source: string[] };

export interface MarkdownLine {
	spans: MarkdownSpan[];
}

function parseLine(line: string): MarkdownSpan[] {
	const out: MarkdownSpan[] = [];
	let i = 0;
	while (i < line.length) {
		if (line[i] === "`") {
			const end = line.indexOf("`", i + 1);
			if (end > i) {
				out.push({ kind: "text", text: line.slice(i + 1, end), code: true });
				i = end + 1;
				continue;
			}
			out.push({ kind: "text", text: "`" });
			i++;
			continue;
		}
		if (line.startsWith("**", i)) {
			const end = line.indexOf("**", i + 2);
			if (end > i) {
				out.push({ kind: "text", text: line.slice(i + 2, end), bold: true });
				i = end + 2;
				continue;
			}
			out.push({ kind: "text", text: "*" });
			i++;
			continue;
		}
		if (line[i] === "*" && line[i + 1] !== "*") {
			const end = line.indexOf("*", i + 1);
			if (end > i) {
				out.push({ kind: "text", text: line.slice(i + 1, end), italic: true });
				i = end + 1;
				continue;
			}
			out.push({ kind: "text", text: "*" });
			i++;
			continue;
		}
		const next = nextSpecial(line, i);
		if (next <= i) {
			out.push({ kind: "text", text: line[i]! });
			i++;
			continue;
		}
		out.push({ kind: "text", text: line.slice(i, next) });
		i = next;
	}
	return out.filter((span) => !(span.kind === "text" && span.text === ""));
}

function nextSpecial(line: string, start: number): number {
	let i = start;
	while (i < line.length && line[i] !== "`" && line[i] !== "*") i++;
	return i;
}

function splitTableRow(line: string): string[] | undefined {
	const trimmed = line.trim();
	if (!trimmed.includes("|")) return undefined;
	const content = trimmed.replace(/^\|/, "").replace(/\|$/, "");
	const cells: string[] = [];
	let cell = "";
	let code = false;
	for (let i = 0; i < content.length; i++) {
		const char = content[i]!;
		if (char === "`" && content[i - 1] !== "\\") code = !code;
		if (char === "|" && !code && content[i - 1] !== "\\") {
			cells.push(cell.trim());
			cell = "";
		} else {
			cell += char;
		}
	}
	cells.push(cell.trim());
	return cells.map((value) => value.replace(/\\\|/g, "|"));
}

function parseDelimiter(line: string): TableAlignment[] | undefined {
	const cells = splitTableRow(line);
	if (!cells || cells.length === 0) return undefined;
	const alignments: TableAlignment[] = [];
	for (const cell of cells) {
		if (!/^:?-{3,}:?$/.test(cell)) return undefined;
		alignments.push(cell.startsWith(":") && cell.endsWith(":") ? "center" : cell.endsWith(":") ? "right" : "left");
	}
	return alignments;
}

export function renderMarkdown(input: string): MarkdownLine[] {
	const lines = input.split("\n");
	const out: MarkdownLine[] = [];
	let i = 0;
	while (i < lines.length) {
		const line = lines[i]!;
		const fence = /^```([\w-]*)\s*$/.exec(line);
		if (fence) {
			const block: string[] = [];
			i++;
			while (i < lines.length && !/^```\s*$/.test(lines[i]!)) block.push(lines[i++]!);
			if (i < lines.length) i++;
			out.push({ spans: [{ kind: "code-block", lang: (fence[1] ?? "").toLowerCase(), text: block.join("\n") }] });
			continue;
		}
		const headerCells = splitTableRow(line);
		const alignments = i + 1 < lines.length ? parseDelimiter(lines[i + 1]!) : undefined;
		if (headerCells && alignments && headerCells.length === alignments.length) {
			const rows = [headerCells];
			const source = [line, lines[i + 1]!];
			i += 2;
			while (i < lines.length) {
				const row = splitTableRow(lines[i]!);
				if (!row || row.length !== alignments.length) break;
				rows.push(row);
				source.push(lines[i]!);
				i++;
			}
			out.push({ spans: [{ kind: "table", rows, alignments, source }] });
			continue;
		}
		const header = /^(#{1,6})\s+(.+)$/.exec(line);
		if (header) out.push({ spans: [{ kind: "text", text: header[2]!, bold: true }] });
		else {
			const quote = /^>\s*(.*)$/.exec(line);
			const unordered = /^[-*]\s+(.+)$/.exec(line);
			const ordered = /^\d+\.\s+(.+)$/.exec(line);
			if (quote) out.push({ spans: [{ kind: "text", text: `│ ${quote[1] ?? ""}` }] });
			else if (unordered) out.push({ spans: parseLine(`• ${unordered[1]!}`) });
			else if (ordered) out.push({ spans: parseLine(ordered[1]!) });
			else if (/^---+\s*$/.test(line) || /^\*\*\*+\s*$/.test(line)) out.push({ spans: [{ kind: "text", text: "─".repeat(20) }] });
			else out.push({ spans: parseLine(line) });
		}
		i++;
	}
	return out;
}

export function markdownToPlain(input: string): string {
	const out: string[] = [];
	for (const line of renderMarkdown(input)) {
		let text = "";
		for (const span of line.spans) {
			if (span.kind === "code-block") out.push(span.text);
			else if (span.kind === "table") out.push(...renderTable(span, Number.POSITIVE_INFINITY));
			else text += span.text;
		}
		if (text) out.push(text);
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
const FG_MAGENTA = `${ESC}[35m`;
const BG_DIM = `${ESC}[48;5;236m`;
const RESET = `${ESC}[0m`;
const OSC8_CLOSE = `${ESC}]8;;\x07`;

const LANGUAGE: Record<string, "js" | "python" | "bash"> = {
	ts: "js",
	tsx: "js",
	typescript: "js",
	js: "js",
	jsx: "js",
	javascript: "js",
	py: "python",
	python: "python",
	bash: "bash",
	sh: "bash",
	shell: "bash",
	zsh: "bash",
};

const KEYWORDS = {
	js: new Set("as async await break case catch class const continue debugger default delete do else enum export extends false finally for from function get if implements import in instanceof interface let new null of package private protected public return set static super switch this throw true try type typeof undefined var void while with yield".split(" ")),
	python: new Set("and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield".split(" ")),
	bash: new Set("case do done elif else esac export fi for function if in local readonly return select then time until while".split(" ")),
};

interface ControlToken {
	text: string;
	end: number;
	oscOpen?: string;
	oscClose?: boolean;
	sgr?: boolean;
}

function controlAt(text: string, index: number): ControlToken | undefined {
	if (text.charCodeAt(index) !== 0x1b) return undefined;
	if (text[index + 1] === "[") {
		let end = index + 2;
		while (end < text.length && !(text.charCodeAt(end) >= 0x40 && text.charCodeAt(end) <= 0x7e)) end++;
		if (end >= text.length) return { text: text.slice(index), end: text.length };
		const value = text.slice(index, end + 1);
		return { text: value, end: end + 1, sgr: value.endsWith("m") };
	}
	if (text[index + 1] === "]") {
		let end = index + 2;
		while (end < text.length && text.charCodeAt(end) !== 0x07 && !(text.charCodeAt(end) === 0x1b && text[end + 1] === "\\")) end++;
		if (end >= text.length) return { text: text.slice(index), end: text.length };
		end += text.charCodeAt(end) === 0x07 ? 1 : 2;
		const value = text.slice(index, end);
		if (value.startsWith(`${ESC}]8;;`)) return { text: value, end, oscClose: value === OSC8_CLOSE || value === `${ESC}]8;;${ESC}\\`, oscOpen: value === OSC8_CLOSE || value === `${ESC}]8;;${ESC}\\` ? undefined : value };
		return { text: value, end };
	}
	return { text: text.slice(index, index + 2), end: Math.min(text.length, index + 2) };
}

function graphemes(text: string): string[] {
	const Segmenter = (globalThis as any).Intl?.Segmenter as (new (locale?: string, options?: { granularity: "grapheme" }) => { segment(value: string): Iterable<{ segment: string }> }) | undefined;
	return Segmenter ? [...new Segmenter("en", { granularity: "grapheme" }).segment(text)].map((part) => part.segment) : Array.from(text);
}

function charWidth(value: string): number {
	let width = 0;
	for (const char of value) {
		const cp = char.codePointAt(0)!;
		if (/\p{Mark}/u.test(char) || cp === 0x200d || cp === 0xfe0f || (cp >= 0x200b && cp <= 0x200f)) continue;
		width += cp >= 0x1100 && (cp <= 0x115f || cp >= 0x2e80 && cp <= 0xa4cf || cp >= 0xac00 && cp <= 0xd7a3 || cp >= 0xf900 && cp <= 0xfaff || cp >= 0xfe30 && cp <= 0xfe6f || cp >= 0xff00 && cp <= 0xff60 || cp >= 0x1f300 && cp <= 0x1faff) ? 2 : 1;
	}
	return width;
}

export function markdownVisibleWidth(text: string): number {
	let plain = "";
	for (let i = 0; i < text.length;) {
		const control = controlAt(text, i);
		if (control) i = control.end;
		else {
			plain += text[i]!;
			i++;
		}
	}
	return graphemes(plain).reduce((sum, value) => sum + charWidth(value), 0);
}

export function markdownTruncateToWidth(text: string, width: number, ellipsis = ""): string {
	if (markdownVisibleWidth(text) <= width) return text;
	const target = Math.max(0, width - charWidth(ellipsis));
	let out = "";
	let used = 0;
	let activeLink: string | undefined;
	let sawSgr = false;
	for (let i = 0; i < text.length;) {
		const control = controlAt(text, i);
		if (control) {
			out += control.text;
			if (control.oscOpen) activeLink = control.oscOpen;
			if (control.oscClose) activeLink = undefined;
			if (control.sgr) sawSgr = true;
			i = control.end;
			continue;
		}
		const nextControl = text.indexOf(ESC, i);
		const end = nextControl < 0 ? text.length : nextControl;
		let consumed = 0;
		for (const value of graphemes(text.slice(i, end))) {
			const valueWidth = charWidth(value);
			if (used + valueWidth > target) {
				i = text.length;
				break;
			}
			out += value;
			used += valueWidth;
			consumed += value.length;
		}
		if (i !== text.length) i += consumed;
	}
	out += ellipsis;
	if (activeLink) out += OSC8_CLOSE;
	if (sawSgr && !out.endsWith(RESET)) out += RESET;
	return out;
}

function linkify(text: string): string {
	return text.replace(/https?:\/\/[^\s<>]+/g, (match) => {
		let url = match;
		let suffix = "";
		while (/[),.;!?\]}]$/.test(url)) {
			suffix = url.slice(-1) + suffix;
			url = url.slice(0, -1);
		}
		return `${ESC}]8;;${url}\x07${url}${OSC8_CLOSE}${suffix}`;
	});
}

function highlightCode(code: string, lang: string): string {
	const family = LANGUAGE[lang];
	if (!family) return linkify(code);
	let out = "";
	let i = 0;
	while (i < code.length) {
		const char = code[i]!;
		if ((family === "js" && code.startsWith("//", i)) || (family === "python" && char === "#") || (family === "bash" && char === "#")) {
			out += `${DIM}${linkify(code.slice(i))}${RESET}`;
			break;
		}
		if (char === "'" || char === '"' || family === "js" && char === "`") {
			const quote = char;
			let end = i + 1;
			while (end < code.length) {
				if (code[end] === "\\") end += 2;
				else if (code[end++] === quote) break;
			}
			out += `${FG_GREEN}${linkify(code.slice(i, end))}${RESET}`;
			i = end;
			continue;
		}
		if (family === "js" && code.startsWith("/*", i)) {
			const end = code.indexOf("*/", i + 2);
			const cut = end < 0 ? code.length : end + 2;
			out += `${DIM}${linkify(code.slice(i, cut))}${RESET}`;
			i = cut;
			continue;
		}
		const identifier = /^[A-Za-z_$][\w$]*/.exec(code.slice(i));
		if (identifier) {
			const value = identifier[0];
			out += KEYWORDS[family].has(value) ? `${FG_MAGENTA}${value}${RESET}` : value;
			i += value.length;
			continue;
		}
		const number = /^\d+(?:\.\d+)?/.exec(code.slice(i));
		if (number) {
			out += `${FG_YELLOW}${number[0]}${RESET}`;
			i += number[0].length;
			continue;
		}
		out += char;
		i++;
	}
	return out;
}

function padCell(value: string, width: number, alignment: TableAlignment): string {
	const clipped = markdownTruncateToWidth(value, width, "…");
	const padding = Math.max(0, width - markdownVisibleWidth(clipped));
	const left = alignment === "right" ? padding : alignment === "center" ? Math.floor(padding / 2) : 0;
	return `${" ".repeat(left)}${clipped}${" ".repeat(padding - left)}`;
}

function renderTable(table: Extract<MarkdownSpan, { kind: "table" }>, available: number): string[] {
	const columns = table.alignments.length;
	const minimum = columns * 3 + 1;
	if (Number.isFinite(available) && available < minimum) return table.source;
	const widths = table.alignments.map((_, column) => Math.max(1, ...table.rows.map((row) => markdownVisibleWidth(row[column] ?? ""))));
	if (Number.isFinite(available)) {
		let total = widths.reduce((sum, width) => sum + width, 0) + columns * 3 + 1;
		while (total > available) {
			let widest = -1;
			for (let column = 0; column < widths.length; column++) if (widths[column]! > 1 && (widest < 0 || widths[column]! > widths[widest]!)) widest = column;
			if (widest < 0) return table.source;
			widths[widest]!--;
			total--;
		}
	}
	const border = (left: string, middle: string, right: string) => left + widths.map((width) => "─".repeat(width + 2)).join(middle) + right;
	const lines = [border("┌", "┬", "┐")];
	for (let row = 0; row < table.rows.length; row++) {
		lines.push(`│ ${table.rows[row]!.map((cell, column) => padCell(cell, widths[column]!, table.alignments[column]!)).join(" │ ")} │`);
		if (row === 0) lines.push(border("├", "┼", "┤"));
	}
	lines.push(border("└", "┴", "┘"));
	return lines;
}

export interface ColoredLine {
	text: string;
}

export interface RenderColoredOptions {
	wrapWidth?: number;
}

export function markdownSliceByWidth(text: string, width: number): { head: string; tail: string } {
	let head = "";
	let used = 0;
	let activeLink: string | undefined;
	for (let i = 0; i < text.length;) {
		const control = controlAt(text, i);
		if (control) {
			head += control.text;
			if (control.oscOpen) activeLink = control.oscOpen;
			if (control.oscClose) activeLink = undefined;
			i = control.end;
			continue;
		}
		const nextControl = text.indexOf(ESC, i);
		const end = nextControl < 0 ? text.length : nextControl;
		for (const value of graphemes(text.slice(i, end))) {
			const valueWidth = charWidth(value);
			if (used + valueWidth > width) {
				return {
					head: head + (activeLink ? OSC8_CLOSE : ""),
					tail: (activeLink ?? "") + text.slice(i),
				};
			}
			head += value;
			used += valueWidth;
			i += value.length;
		}
	}
	return { head, tail: "" };
}

function wrapColored(text: string, width: number): string[] {
	if (width < 1 || markdownVisibleWidth(text) <= width) return [text];
	const lines: string[] = [];
	let rest = text;
	while (markdownVisibleWidth(rest) > width) {
		const { head, tail } = markdownSliceByWidth(rest, width);
		if (!head || tail === rest) break;
		lines.push(head.trimEnd());
		rest = tail;
	}
	if (rest) lines.push(rest.trimEnd());
	return lines;
}

export function renderMarkdownColored(input: string, opts: RenderColoredOptions = {}): string[] {
	const out: string[] = [];
	for (const line of renderMarkdown(input)) {
		if (line.spans.length === 1 && line.spans[0]?.kind === "code-block") {
			const span = line.spans[0];
			for (const codeLine of span.text.split("\n")) out.push(`${BG_DIM}  ${highlightCode(codeLine, span.lang)}${RESET}`);
			continue;
		}
		if (line.spans.length === 1 && line.spans[0]?.kind === "table") {
			out.push(...renderTable(line.spans[0], opts.wrapWidth ?? Number.POSITIVE_INFINITY));
			continue;
		}
		let text = "";
		for (const span of line.spans) {
			if (span.kind !== "text") continue;
			let segment = linkify(span.text);
			if (span.bold) segment = `${BOLD}${segment}${RESET}`;
			if (span.italic) segment = `${DIM}${segment}${RESET}`;
			if (span.code) segment = `${FG_CYAN}${segment}${RESET}`;
			text += segment;
		}
		out.push(...(opts.wrapWidth ? wrapColored(text, opts.wrapWidth) : [text]));
	}
	return out;
}
