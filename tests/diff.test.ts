import { describe, expect, it } from "vitest";
import {
	diffChunks,
	diffLines,
	diffStats,
	formatDiff,
	formatToolDiff,
	toolDiffEdits,
	type DiffChunk,
	type DiffSkip,
} from "../src/diff.ts";

function ops(lines: ReturnType<typeof diffLines>): string[] {
	return (lines ?? []).map((l) => `${l.op}:${l.text}`);
}

function skips(chunks: DiffChunk[]): DiffSkip[] {
	return chunks.filter((chunk): chunk is DiffSkip => chunk.op === "skip");
}

describe("diffLines", () => {
	it("reports untouched text as all context", () => {
		const lines = diffLines("a\nb\nc", "a\nb\nc")!;
		expect(lines).toHaveLength(3);
		expect(lines.every((l) => l.op === "context")).toBe(true);
	});

	it("marks a single changed line in the middle", () => {
		expect(ops(diffLines("one\ntwo\nthree", "one\nTWO\nthree"))).toEqual([
			"context:one",
			"remove:two",
			"add:TWO",
			"context:three",
		]);
		const lines = diffLines("one\ntwo\nthree", "one\nTWO\nthree")!;
		expect(lines[1]!.oldLine).toBe(2);
		expect(lines[2]!.newLine).toBe(2);
	});

	it("handles insertions and deletions", () => {
		expect(diffLines("a\nb", "a\nx\nb")!.map((l) => l.op)).toEqual(["context", "add", "context"]);
		expect(diffLines("a\nx\nb", "a\nb")!.map((l) => l.op)).toEqual(["context", "remove", "context"]);
	});

	it("diffs from and to empty text", () => {
		expect(diffLines("", "a\nb")!.map((l) => l.op)).toEqual(["add", "add"]);
		expect(diffLines("a\nb", "")!.map((l) => l.op)).toEqual(["remove", "remove"]);
		expect(diffLines("", "")!).toEqual([]);
	});

	it("keeps duplicate lines that match on both sides", () => {
		// The naive "line is not in the other side" approach drops every blank
		// line; a real LCS keeps the ones that genuinely match.
		expect(ops(diffLines("a\n\nb", "a\n\nc"))).toEqual(["context:a", "context:", "remove:b", "add:c"]);
	});

	it("trims the common prefix and suffix before running the LCS", () => {
		const before = Array.from({ length: 500 }, (_, i) => `line-${i}`).join("\n");
		const after = before.replace("line-250", "line-250-changed");
		const lines = diffLines(before, after)!;
		expect(lines.filter((l) => l.op !== "context")).toHaveLength(2);
		expect(lines.filter((l) => l.op === "add")[0]!.newLine).toBe(251);
	});

	it("returns undefined instead of trying to diff huge inputs", () => {
		const big = Array.from({ length: 20_000 }, (_, i) => `line-${i}`).join("\n");
		const other = Array.from({ length: 20_000 }, (_, i) => `other-${i}`).join("\n");
		expect(diffLines(big, other)).toBeUndefined();
		expect(diffChunks(big, other)).toEqual([]);
		expect(formatDiff(big, other)).toEqual([]);
		expect(diffStats(big, other)).toBeUndefined();
	});
});

describe("diffChunks", () => {
	it("collapses unchanged runs into skip markers", () => {
		const before = Array.from({ length: 30 }, (_, i) => `line-${i}`).join("\n");
		const after = before.replace("line-15", "changed");
		const chunks = diffChunks(before, after, { context: 1 });
		expect(skips(chunks)).toHaveLength(2);
		expect(chunks.some((c) => c.op === "remove" && c.text === "line-15")).toBe(true);
		expect(chunks.some((c) => c.op === "add" && c.text === "changed")).toBe(true);
	});

	it("does not collapse when there is nothing to collapse", () => {
		expect(skips(diffChunks("a\nb", "a\nc", { context: 1 }))).toHaveLength(0);
	});

	it("caps output and folds the dropped tail into one truncated marker", () => {
		const before = Array.from({ length: 40 }, (_, i) => `old-${i}`).join("\n");
		const after = Array.from({ length: 40 }, (_, i) => `new-${i}`).join("\n");
		const chunks = diffChunks(before, after, { context: 1, maxLines: 6 });
		expect(chunks).toHaveLength(6);
		const last = chunks[chunks.length - 1]!;
		expect(last.op).toBe("skip");
		expect(last).toEqual({ op: "skip", count: 75, truncated: true }); // 80 ops − 5 kept
	});
});

describe("formatDiff", () => {
	it("prefixes context, additions, and removals", () => {
		expect(formatDiff("a\nb", "a\nc")).toEqual(["  a", "- b", "+ c"]);
	});

	it("labels elided runs and truncated tails differently", () => {
		const before = Array.from({ length: 20 }, (_, i) => `line-${i}`).join("\n");
		const after = before.replace("line-10", "changed");
		expect(formatDiff(before, after, { context: 1 }).some((line) => line.startsWith("⋮"))).toBe(true);

		const big = Array.from({ length: 40 }, (_, i) => `o${i}`).join("\n");
		const other = Array.from({ length: 40 }, (_, i) => `n${i}`).join("\n");
		expect(formatDiff(big, other, { maxLines: 4 }).at(-1)).toContain("…");
	});
});

describe("hunk headers", () => {
	it("omits coordinates for a single contiguous change", () => {
		expect(formatDiff("a\nb", "a\nc")).toEqual(["  a", "- b", "+ c"]);
	});

	it("labels each hunk when a change is split across a file", () => {
		const before = Array.from({ length: 30 }, (_, i) => `line-${i}`).join("\n");
		const after = before.replace("line-5", "FIVE").replace("line-25", "TWENTYFIVE");
		const lines = formatDiff(before, after, { context: 1 });
		const headers = lines.filter((l) => l.startsWith("@@"));
		expect(headers).toEqual(["@@ -5,3 +5,3 @@", "@@ -25,3 +25,3 @@"]);
	});

	it("counts zero old lines for a pure insertion", () => {
		const before = Array.from({ length: 20 }, (_, i) => `line-${i}`).join("\n");
		const after = `${before}\nappended`;
		const header = formatDiff(before, after, { context: 0 }).find((l) => l.startsWith("@@"));
		expect(header).toBe("@@ -20,0 +21,1 @@");
	});

	it("counts zero new lines for a pure deletion", () => {
		const before = Array.from({ length: 20 }, (_, i) => `line-${i}`).join("\n");
		const after = before.replace("line-7\n", "");
		const header = formatDiff(before, after, { context: 0 }).find((l) => l.startsWith("@@"));
		expect(header).toBe("@@ -8,1 +7,0 @@");
	});

	it("needs no coordinates when the whole file is new", () => {
		// One contiguous run, nothing elided — the header would be noise.
		expect(formatDiff("", "a\nb\nc", { context: 0 })).toEqual(["+ a", "+ b", "+ c"]);
	});
});

describe("toolDiffEdits / formatToolDiff", () => {
	it("normalizes snake_case tool names", () => {
		const details = { edits: [{ path: "a.ts", oldText: "one", newText: "uno" }] };
		expect(toolDiffEdits("multi_edit", details)).toHaveLength(1);
		expect(toolDiffEdits("multiEdit", details)).toHaveLength(1);
		expect(toolDiffEdits("multi_edit", { changes: [{ oldText: "x", newText: "y" }] })).toHaveLength(1);
	});

	it("ignores tools that do not carry a diff payload", () => {
		expect(toolDiffEdits("bash", { code: 0 })).toEqual([]);
		expect(toolDiffEdits("read", { totalLines: 3 })).toEqual([]);
		expect(toolDiffEdits("write", undefined)).toEqual([]);
		expect(formatToolDiff("bash", { code: 0 })).toEqual([]);
	});

	it("prefixes each file when a tool touched several", () => {
		const out = formatToolDiff("multi_edit", {
			edits: [
				{ path: "a.ts", oldText: "one", newText: "uno" },
				{ path: "b.ts", oldText: "two", newText: "dos" },
			],
		});
		expect(out).toEqual(["a.ts", "- one", "+ uno", "b.ts", "- two", "+ dos"]);
	});

	it("skips edits that changed nothing", () => {
		expect(formatToolDiff("edit", { path: "a.ts", oldText: "same", newText: "same" })).toEqual([]);
	});
});

describe("diffStats", () => {
	it("counts added and removed lines", () => {
		expect(diffStats("a\nb\nc", "a\nB\nc\nd")).toEqual({ added: 2, removed: 1 });
		expect(diffStats("same", "same")).toEqual({ added: 0, removed: 0 });
	});
});

/* ------------------------------------------------------------------ *
 * Correctness invariants                                              *
 * ------------------------------------------------------------------ */

/** Reference LCS length, used only to check that our diff is minimal. */
function lcsLength(a: string[], b: string[]): number {
	const dp: number[][] = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
	for (let i = a.length - 1; i >= 0; i--) {
		for (let j = b.length - 1; j >= 0; j--) {
			dp[i]![j] = a[i] === b[j] ? dp[i + 1]![j + 1]! + 1 : Math.max(dp[i + 1]![j]!, dp[i]![j + 1]!);
		}
	}
	return dp[0]![0]!;
}

/**
 * The project's line-splitting rule: an empty file has zero lines, not one
 * blank one. The reference model below must use the same rule or the
 * minimality check compares against a different input than the diff saw —
 * `[""].join("\n")` is `""`, which is zero lines, not one.
 */
function splitForReference(text: string): string[] {
	return text.length === 0 ? [] : text.split("\n");
}

/** Small deterministic PRNG so any failure is reproducible. */
function mulberry32(seed: number): () => number {
	let state = seed;
	return () => {
		state = (state + 0x6d2b79f5) | 0;
		let t = Math.imul(state ^ (state >>> 15), 1 | state);
		t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

describe("correctness invariants", () => {
	it("reproduces both sides and stays minimal across randomized inputs", () => {
		const random = mulberry32(20260830);
		// A tiny alphabet forces heavy duplication — blank lines, repeated
		// imports, identical `}` lines. This is exactly where a naive
		// "line not in the other side" diff falls apart.
		const alphabet = ["a", "b", "}", "", "const x = 1;"];
		for (let iteration = 0; iteration < 1_000; iteration++) {
			const pick = (n: number) => Array.from({ length: n }, () => alphabet[Math.floor(random() * alphabet.length)]!);
			const oldText = pick(Math.floor(random() * 9)).join("\n");
			const newText = pick(Math.floor(random() * 9)).join("\n");
			// Derive the reference arrays from the text, not from the generated
			// arrays, so both sides agree on how "" is counted.
			const a = splitForReference(oldText);
			const b = splitForReference(newText);

			const lines = diffLines(oldText, newText)!;
			expect(lines).toBeDefined();

			// Applying the ops must reconstruct both sides exactly.
			const rebuiltNew = lines.filter((l) => l.op !== "remove").map((l) => l.text).join("\n");
			const rebuiltOld = lines.filter((l) => l.op !== "add").map((l) => l.text).join("\n");
			expect(rebuiltNew).toBe(newText);
			expect(rebuiltOld).toBe(oldText);

			// The edit script must be minimal: added + removed == |a| + |b| - 2·LCS.
			const stats = diffStats(oldText, newText)!;
			expect(stats.added + stats.removed).toBe(a.length + b.length - 2 * lcsLength(a, b));
		}
	});

	it("keeps line numbers consistent with the reconstructed text", () => {
		const oldText = "one\ntwo\nthree\nfour\nfive";
		const newText = "one\ntwo-changed\nthree\nfour\nfive\nsix";
		const lines = diffLines(oldText, newText)!;
		for (const line of lines) {
			if (line.oldLine !== undefined) expect(oldText.split("\n")[line.oldLine - 1]).toBe(line.text);
			if (line.newLine !== undefined) expect(newText.split("\n")[line.newLine - 1]).toBe(line.text);
		}
	});

	it("stays fast on a realistic large file", () => {
		const before = Array.from({ length: 5_000 }, (_, i) => `export const value${i} = ${i};`).join("\n");
		const after = before.replace("export const value2500 = 2500;", "export const value2500 = 2501;");
		const started = Date.now();
		const chunks = diffChunks(before, after);
		const elapsed = Date.now() - started;
		expect(diffStats(before, after)).toEqual({ added: 1, removed: 1 });
		expect(chunks.some((c) => c.op === "hunk")).toBe(true);
		// Generous bound: prefix/suffix trimming should make this near-instant,
		// and this catches a regression back to a full O(n·m) table.
		expect(elapsed).toBeLessThan(2_000);
	});
});
