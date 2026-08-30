/**
 * Line-oriented diff used to render `write` / `edit` / `multi_edit` tool
 * results as readable hunks instead of dumping whole files into the chat.
 *
 * Deliberately dependency-free. A full Myers diff is overkill for chat
 * display: we trim the common prefix/suffix first (for a normal edit that
 * leaves a handful of lines) and then run a plain LCS table over the changed
 * region only. Inputs too large for that table are not diffed at all — the
 * caller falls back to a byte/line-count summary rather than freezing the UI.
 */

/** What happened to one line: unchanged, inserted, or deleted. */
export type DiffOp = "context" | "add" | "remove";

export interface DiffLine {
	op: DiffOp;
	text: string;
	/** 1-based line number in the old text, when the line exists there. */
	oldLine?: number;
	/** 1-based line number in the new text, when the line exists there. */
	newLine?: number;
}

/** A run of unchanged lines that was elided from the display. */
export interface DiffSkip {
	op: "skip";
	count: number;
	/** True when the run was dropped because the output hit `maxLines`. */
	truncated?: boolean;
}

/**
 * A unified-diff-style header (`@@ -12,3 +12,4 @@`) marking where a run of
 * changes landed. Only emitted when the diff is split into several hunks or
 * has elided context — a single small change needs no coordinates.
 */
export interface DiffHunk {
	op: "hunk";
	oldStart: number;
	oldCount: number;
	newStart: number;
	newCount: number;
}

export type DiffChunk = DiffLine | DiffSkip | DiffHunk;

export interface DiffOptions {
	/** Unchanged lines kept on each side of a change. Default 1. */
	context?: number;
	/** Hard cap on emitted lines (skip markers count). Default 40. */
	maxLines?: number;
	/** LCS table budget in cells; larger inputs are not diffed. Default 250k. */
	maxCells?: number;
}

export interface DiffStats {
	added: number;
	removed: number;
}

const DEFAULT_CONTEXT = 1;
const DEFAULT_MAX_LINES = 40;
const DEFAULT_MAX_CELLS = 250_000;

function splitLines(text: string): string[] {
	return text.length === 0 ? [] : text.split("\n");
}

/**
 * LCS length table over `a` × `b`, filled back-to-front so entry (0,0) is the
 * full LCS length. Returns `undefined` when the table would exceed `maxCells`.
 */
function lcsTable(a: readonly string[], b: readonly string[], maxCells: number): Uint32Array | undefined {
	const width = b.length + 1;
	const cells = (a.length + 1) * width;
	if (cells > maxCells) return undefined;
	const table = new Uint32Array(cells);
	for (let i = a.length - 1; i >= 0; i--) {
		for (let j = b.length - 1; j >= 0; j--) {
			table[i * width + j] =
				a[i] === b[j]
					? table[(i + 1) * width + (j + 1)]! + 1
					: Math.max(table[(i + 1) * width + j]!, table[i * width + (j + 1)]!);
		}
	}
	return table;
}

/** Walk the LCS table forward into a flat list of ops. */
function walkTable(a: readonly string[], b: readonly string[], table: Uint32Array): DiffLine[] {
	const width = b.length + 1;
	const out: DiffLine[] = [];
	let i = 0;
	let j = 0;
	while (i < a.length && j < b.length) {
		if (a[i] === b[j]) {
			out.push({ op: "context", text: a[i]! });
			i++;
			j++;
		} else if (table[(i + 1) * width + j]! >= table[i * width + (j + 1)]!) {
			out.push({ op: "remove", text: a[i]! });
			i++;
		} else {
			out.push({ op: "add", text: b[j]! });
			j++;
		}
	}
	while (i < a.length) out.push({ op: "remove", text: a[i++]! });
	while (j < b.length) out.push({ op: "add", text: b[j++]! });
	return out;
}

/**
 * Diff two blobs of text line by line.
 *
 * Returns `undefined` when the input is too large to diff safely — callers
 * should treat that as "no preview available", not as "no changes".
 */
export function diffLines(oldText: string, newText: string, opts: DiffOptions = {}): DiffLine[] | undefined {
	const maxCells = opts.maxCells ?? DEFAULT_MAX_CELLS;
	const a = splitLines(oldText);
	const b = splitLines(newText);

	// Trim the common prefix and suffix: for a typical edit the interesting
	// region is a few lines no matter how big the file is.
	let start = 0;
	while (start < a.length && start < b.length && a[start] === b[start]) start++;
	let endA = a.length;
	let endB = b.length;
	while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
		endA--;
		endB--;
	}

	const table = lcsTable(a.slice(start, endA), b.slice(start, endB), maxCells);
	if (!table) return undefined;
	const middle = walkTable(a.slice(start, endA), b.slice(start, endB), table);

	const out: DiffLine[] = [];
	for (let i = 0; i < start; i++) out.push({ op: "context", text: a[i]!, oldLine: i + 1, newLine: i + 1 });
	let oldIndex = start;
	let newIndex = start;
	for (const line of middle) {
		if (line.op === "context") {
			out.push({ op: "context", text: line.text, oldLine: oldIndex + 1, newLine: newIndex + 1 });
			oldIndex++;
			newIndex++;
		} else if (line.op === "remove") {
			out.push({ op: "remove", text: line.text, oldLine: oldIndex + 1 });
			oldIndex++;
		} else {
			out.push({ op: "add", text: line.text, newLine: newIndex + 1 });
			newIndex++;
		}
	}
	for (let i = endA; i < a.length; i++) {
		out.push({ op: "context", text: a[i]!, oldLine: i + 1, newLine: endB + (i - endA) + 1 });
	}
	return out;
}

/** Trim the output to `maxLines`, folding the dropped tail into one marker. */
function capChunks(chunks: DiffChunk[], maxLines: number): DiffChunk[] {
	if (maxLines <= 0 || chunks.length <= maxLines) return chunks;
	const kept = chunks.slice(0, Math.max(0, maxLines - 1));
	let remaining = 0;
	for (const chunk of chunks.slice(kept.length)) remaining += chunk.op === "skip" ? chunk.count : 1;
	if (remaining > 0) kept.push({ op: "skip", count: remaining, truncated: true });
	return kept;
}

/** Compute the `@@ -a,b +c,d @@` coordinates for one run of changed lines. */
function hunkHeaderFor(run: DiffLine[]): DiffHunk {
	let oldStart = Number.POSITIVE_INFINITY;
	let oldEnd = Number.NEGATIVE_INFINITY;
	let newStart = Number.POSITIVE_INFINITY;
	let newEnd = Number.NEGATIVE_INFINITY;
	for (const line of run) {
		if (line.oldLine !== undefined) {
			oldStart = Math.min(oldStart, line.oldLine);
			oldEnd = Math.max(oldEnd, line.oldLine);
		}
		if (line.newLine !== undefined) {
			newStart = Math.min(newStart, line.newLine);
			newEnd = Math.max(newEnd, line.newLine);
		}
	}
	// A pure insertion has no old lines: git reports the position *before* the
	// insert with a count of 0. Same idea in reverse for a pure deletion.
	if (!Number.isFinite(oldStart)) {
		oldStart = Number.isFinite(newStart) ? Math.max(0, newStart - 1) : 0;
		oldEnd = oldStart - 1;
	}
	if (!Number.isFinite(newStart)) {
		newStart = Math.max(0, oldStart - 1);
		newEnd = newStart - 1;
	}
	return {
		op: "hunk",
		oldStart,
		oldCount: Math.max(0, oldEnd - oldStart + 1),
		newStart,
		newCount: Math.max(0, newEnd - newStart + 1),
	};
}

/** Insert a hunk header before each run of changed lines. */
function addHunkHeaders(chunks: (DiffLine | DiffSkip)[]): DiffChunk[] {
	let runs = 0;
	let elided = false;
	for (let i = 0; i < chunks.length; i++) {
		if (chunks[i]!.op === "skip") {
			elided = true;
			continue;
		}
		if (i === 0 || chunks[i - 1]!.op === "skip") runs++;
	}
	// One contiguous change with nothing elided: the coordinates are noise.
	if (runs <= 1 && !elided) return chunks;

	const out: DiffChunk[] = [];
	let i = 0;
	while (i < chunks.length) {
		if (chunks[i]!.op === "skip") {
			out.push(chunks[i]!);
			i++;
			continue;
		}
		const run: DiffLine[] = [];
		while (i < chunks.length && chunks[i]!.op !== "skip") run.push(chunks[i++] as DiffLine);
		out.push(hunkHeaderFor(run), ...run);
	}
	return out;
}

/**
 * Diff plus elision: unchanged runs further than `context` lines from any
 * change collapse into a single `skip` marker, and the whole output is capped
 * at `maxLines`. This is the shape the TUI wants — enough to read, never a
 * wall of text.
 */
export function diffChunks(oldText: string, newText: string, opts: DiffOptions = {}): DiffChunk[] {
	const lines = diffLines(oldText, newText, opts);
	if (!lines) return [];
	const context = Math.max(0, opts.context ?? DEFAULT_CONTEXT);

	const keep = new Array<boolean>(lines.length).fill(false);
	for (let i = 0; i < lines.length; i++) {
		if (lines[i]!.op === "context") continue;
		const from = Math.max(0, i - context);
		const to = Math.min(lines.length - 1, i + context);
		for (let j = from; j <= to; j++) keep[j] = true;
	}

	const chunks: (DiffLine | DiffSkip)[] = [];
	let skipped = 0;
	for (let i = 0; i < lines.length; i++) {
		if (!keep[i]) {
			skipped++;
			continue;
		}
		if (skipped > 0) {
			chunks.push({ op: "skip", count: skipped });
			skipped = 0;
		}
		chunks.push(lines[i]!);
	}
	if (skipped > 0) chunks.push({ op: "skip", count: skipped });
	return capChunks(addHunkHeaders(chunks), opts.maxLines ?? DEFAULT_MAX_LINES);
}

/** Render one chunk as plain text (no ANSI) for logs, tests, and non-TTY hosts. */
export function formatDiffChunk(chunk: DiffChunk): string {
	switch (chunk.op) {
		case "add":
			return `+ ${chunk.text}`;
		case "remove":
			return `- ${chunk.text}`;
		case "context":
			return `  ${chunk.text}`;
		case "skip":
			return chunk.truncated ? `… ${chunk.count} more lines` : `⋮ ${chunk.count} unchanged lines`;
		case "hunk":
			return `@@ -${chunk.oldStart},${chunk.oldCount} +${chunk.newStart},${chunk.newCount} @@`;
	}
}

/** Render a whole diff as plain-text lines. Empty when the input is too large. */
export function formatDiff(oldText: string, newText: string, opts: DiffOptions = {}): string[] {
	return diffChunks(oldText, newText, opts).map(formatDiffChunk);
}

/** `+added / -removed` line counts, or `undefined` when the input is too large. */
export function diffStats(oldText: string, newText: string, opts: DiffOptions = {}): DiffStats | undefined {
	const lines = diffLines(oldText, newText, opts);
	if (!lines) return undefined;
	let added = 0;
	let removed = 0;
	for (const line of lines) {
		if (line.op === "add") added++;
		else if (line.op === "remove") removed++;
	}
	return { added, removed };
}

/* ------------------------------------------------------------------ *
 * Tool results                                                        *
 * ------------------------------------------------------------------ */

/** One file's worth of before/after text, as attached to a tool result. */
export interface DiffEdit {
	path?: string;
	oldText?: string;
	newText?: string;
}

/** Tools whose results carry a `{ oldText, newText }` diff payload. */
const DIFF_TOOLS = new Set(["write", "edit", "multiedit"]);

/**
 * Pull the per-file `{ path, oldText, newText }` records out of a tool result.
 *
 * Shared by both renderers so the TUI and the one-shot console never disagree
 * about which results have a diff. Tool names are snake_case (`multi_edit`),
 * so they are normalized before matching.
 */
export function toolDiffEdits(toolName: string, details: unknown): DiffEdit[] {
	if (!details || typeof details !== "object") return [];
	const tool = String(toolName).replace(/_/g, "").toLowerCase();
	if (!DIFF_TOOLS.has(tool)) return [];
	const record = details as Record<string, unknown>;
	const records = tool === "multiedit"
		? (Array.isArray(record) ? record : (record.edits ?? record.changes ?? []))
		: [record];
	return (Array.isArray(records) ? records : []).filter((edit): edit is DiffEdit => !!edit && typeof edit === "object");
}

/**
 * Plain-text diff for a tool result — one block per file, with file paths when
 * the tool touched more than one. Used by the non-TTY console renderer, where
 * ANSI would end up in piped output and log files.
 */
export function formatToolDiff(toolName: string, details: unknown, opts: DiffOptions = {}): string[] {
	const edits = toolDiffEdits(toolName, details);
	const out: string[] = [];
	for (const edit of edits) {
		const { oldText, newText } = edit;
		if (typeof oldText !== "string" || typeof newText !== "string" || oldText === newText) continue;
		if (edits.length > 1 && typeof edit.path === "string") out.push(edit.path);
		out.push(...formatDiff(oldText, newText, opts));
	}
	return out;
}
