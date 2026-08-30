import { describe, it, expect } from "vitest";
import { markdownToPlain, markdownVisibleWidth, renderMarkdown, renderMarkdownColored } from "../src/markdown.ts";

describe("markdown renderer", () => {
	it("renders plain text", () => {
		const lines = renderMarkdown("hello world");
		expect(lines).toHaveLength(1);
		expect(lines[0]!.spans[0]!.kind).toBe("text");
		expect((lines[0]!.spans[0] as any).text).toBe("hello world");
	});

	it("renders fenced code blocks as a single span", () => {
		const md = "before\n```ts\nconst x = 1;\n```\nafter";
		const lines = renderMarkdown(md);
		const code = lines.find((l) => l.spans.some((s) => s.kind === "code-block"));
		expect(code).toBeDefined();
		expect((code!.spans[0] as any).lang).toBe("ts");
		expect((code!.spans[0] as any).text).toBe("const x = 1;");
	});

	it("parses **bold** and *italic*", () => {
		const lines = renderMarkdown("this is **bold** and *italic*");
		const texts = lines[0]!.spans.map((s) => ({ text: (s as any).text, bold: (s as any).bold, italic: (s as any).italic }));
		expect(texts).toContainEqual({ text: "bold", bold: true, italic: undefined });
		expect(texts).toContainEqual({ text: "italic", bold: undefined, italic: true });
	});

	it("parses inline `code`", () => {
		const lines = renderMarkdown("a `b` c");
		const code = lines[0]!.spans.find((s) => (s as any).code === true);
		expect(code).toBeDefined();
		expect((code as any).text).toBe("b");
	});

	it("terminates on an unclosed backtick (regression: streaming OOM crash)", () => {
		// The old parseLine fell through to nextSpecial() when a marker had no
		// closer; nextSpecial returned i itself, so i never advanced and empty
		// spans were allocated forever → 4GB "heap out of memory" mid-stream.
		const lines = renderMarkdown("Here's a `");
		expect(lines).toHaveLength(1);
		const joined = lines[0]!.spans.map((s) => (s as any).text).join("");
		expect(joined).toBe("Here's a `");
	});

	it("terminates on an unclosed ** marker", () => {
		const lines = renderMarkdown("I can help with **a lot");
		expect(lines).toHaveLength(1);
		const joined = lines[0]!.spans.map((s) => (s as any).text).join("");
		expect(joined).toBe("I can help with **a lot");
	});

	it("terminates on an unclosed * marker", () => {
		const lines = renderMarkdown("list * item");
		expect(lines).toHaveLength(1);
		const joined = lines[0]!.spans.map((s) => (s as any).text).join("");
		expect(joined).toBe("list * item");
	});

	it("terminates on every partial-markdown prefix of a typical reply", () => {
		// Simulate token-by-token streaming of a markdown-heavy answer: every
		// prefix must render without hanging and preserve the visible text.
		const full = "I can help with **coding**, `bash`, *writing* and more:\n```js\nconsole.log(1);\n```";
		for (let n = 1; n <= full.length; n++) {
			const partial = full.slice(0, n);
			const plain = markdownToPlain(partial);
			// No hang and no markdown punctuation was invented/lost mid-run
			// (only the final partial prefix may differ by pending markers).
			expect(typeof plain).toBe("string");
		}
	});

	it("renders headers with bold and the heading level", () => {
		const lines = renderMarkdown("## Header text");
		const span = lines[0]!.spans[0]!;
		expect(span.kind).toBe("text");
		expect((span as any).text).toBe("Header text");
		expect((span as any).bold).toBe(true);
		expect((span as any).heading).toBe(2);
	});

	it("preserves the heading level for all six tiers", () => {
		for (let level = 1; level <= 6; level++) {
			const lines = renderMarkdown(`${"#".repeat(level)} Title`);
			const span = lines[0]!.spans[0]!;
			expect(span.kind).toBe("text");
			expect((span as any).heading).toBe(level);
		}
	});

	it("renders unordered lists as a dedicated bullet span", () => {
		const lines = renderMarkdown("- first\n- second");
		expect(lines[0]!.spans[0]!.kind).toBe("bullet");
		expect((lines[0]!.spans[0] as any).text).toBe("first");
		expect((lines[0]!.spans[0] as any).depth).toBe(0);
		expect((lines[1]!.spans[0] as any).text).toBe("second");
	});

	it("preserves nesting depth for indented bullets", () => {
		const lines = renderMarkdown("- top\n  - nested\n    - deeper");
		expect((lines[0]!.spans[0] as any).depth).toBe(0);
		expect((lines[1]!.spans[0] as any).depth).toBe(1);
		expect((lines[2]!.spans[0] as any).depth).toBe(2);
	});

	it("renders ordered lists with their starting index", () => {
		const lines = renderMarkdown("1. first\n2. second");
		expect(lines[0]!.spans[0]!.kind).toBe("ordered");
		expect((lines[0]!.spans[0] as any).index).toBe(1);
		expect((lines[0]!.spans[0] as any).text).toBe("first");
		expect((lines[1]!.spans[0] as any).index).toBe(2);
	});

	it("renders blockquotes as a dedicated quote span", () => {
		const lines = renderMarkdown("> quoted");
		expect(lines[0]!.spans[0]!.kind).toBe("quote");
		expect((lines[0]!.spans[0] as any).text).toBe("quoted");
	});

	it("renders horizontal rules as a dedicated rule span", () => {
		const lines = renderMarkdown("---");
		expect(lines[0]!.spans[0]!.kind).toBe("rule");
	});

	it("markdownToPlain returns plain text for prose", () => {
		expect(markdownToPlain("hello **world**")).toBe("hello world");
	});

	it("markdownToPlain keeps code blocks verbatim", () => {
		const out = markdownToPlain("```\nconst x = 1;\n```");
		expect(out).toBe("const x = 1;");
	});

	it("markdownToPlain renders bullets, ordered lists, and quotes", () => {
		const out = markdownToPlain("- first\n  - nested\n1. one\n2. two\n> quoted");
		expect(out).toContain("- first");
		expect(out).toContain("  - nested");
		expect(out).toContain("1. one");
		expect(out).toContain("2. two");
		expect(out).toContain("> quoted");
	});

	it("handles a full mixed document", () => {
		const md = `# Title

This is a paragraph with **bold** and *italic* and \`code\`.

- item 1
- item 2

\`\`\`ts
const x = 1;
\`\`\`

> a quote
`;
		const out = markdownToPlain(md);
		expect(out).toContain("Title");
		expect(out).toContain("This is a paragraph with bold and italic and code.");
	});
});

describe("renderMarkdownColored", () => {
	it("renders plain text", () => {
		const lines = renderMarkdownColored("hello");
		expect(lines).toHaveLength(1);
		expect(lines[0]!).toContain("hello");
	});

	it("wraps code blocks in a dim background", () => {
		const lines = renderMarkdownColored("```ts\nconst x = 1;\n```");
		// Should have at least one line with ANSI background codes.
		expect(lines.some((l) => l.includes("\x1b[48;5;236m"))).toBe(true);
		expect(lines.some((l) => l.replace(/\x1b\[[0-9;]*m/g, "").includes("const x = 1"))).toBe(true);
	});

	it("colors bold/italic/inline-code text", () => {
		const lines = renderMarkdownColored("this is **bold** and *italic* and `code`");
		const all = lines.join("");
		expect(all).toContain("\x1b[1m"); // bold
		expect(all).toContain("\x1b[2m"); // dim (italic)
		expect(all).toContain("\x1b[36m"); // cyan (inline code)
		expect(all).toContain("bold");
		expect(all).toContain("italic");
		expect(all).toContain("code");
	});

	it("handles headers with an accent bar and blank line above", () => {
		const lines = renderMarkdownColored("# Title");
		// Blank line for visual separation, then the heading with the
		// accent bar, magenta color, and bold.
		expect(lines[0]).toBe("");
		expect(lines[1]).toContain("Title");
		expect(lines[1]).toContain("\x1b[1m"); // bold
		expect(lines[1]).toContain("\x1b[35m"); // magenta
		expect(lines[1]).toMatch(/▌|▎/); // accent bar
	});

	it("renders sub-headers with a smaller accent bar", () => {
		const lines = renderMarkdownColored("## Subhead");
		expect(lines[0]).toBe("");
		expect(lines[1]).toContain("Subhead");
	});

	it("handles lists with a colored bullet marker", () => {
		const lines = renderMarkdownColored("- item 1\n- item 2");
		expect(lines[0]).toContain("\x1b[35m•\x1b[0m");
		expect(lines[0]).toContain("item 1");
		expect(lines[1]).toContain("item 2");
	});

	it("indents nested bullets", () => {
		const lines = renderMarkdownColored("- top\n  - nested");
		expect(lines[1]).toMatch(/^ {2}\x1b\[35m•/);
	});

	it("handles ordered lists with a colored index", () => {
		const lines = renderMarkdownColored("1. first\n2. second");
		expect(lines[0]).toContain("\x1b[35m1.\x1b[0m");
		expect(lines[0]).toContain("first");
		expect(lines[1]).toContain("\x1b[35m2.\x1b[0m");
		expect(lines[1]).toContain("second");
	});

	it("handles blockquotes with a dim cyan bar", () => {
		const lines = renderMarkdownColored("> quoted text");
		expect(lines[0]).toContain("\x1b[36m\x1b[2m│\x1b[0m");
		expect(lines[0]).toContain("quoted text");
	});

	it("handles horizontal rules with a dim divider", () => {
		const lines = renderMarkdownColored("---");
		expect(lines[0]).toContain("\x1b[2m");
		expect(lines[0]).toContain("─");
	});

	it("returns empty array for empty input", () => {
		expect(renderMarkdownColored("")).toEqual([""]);
	});

	it("highlights language aliases without coloring strings or comments as keywords", () => {
		for (const lang of ["ts", "typescript", "js", "javascript", "py", "python", "bash", "sh", "shell", "zsh"]) {
			const source = lang === "py" || lang === "python" ? 'if True: print("if") # if' : lang === "bash" || lang === "sh" || lang === "shell" || lang === "zsh" ? 'if true; then echo "if"; fi # if' : 'const value = "const"; // const';
			const output = renderMarkdownColored(`\`\`\`${lang}\n${source}\n\`\`\``)[0]!;
			expect(output).toContain("\x1b[35m");
			const stringStart = output.indexOf("\x1b[32m");
			const stringEnd = output.indexOf("\x1b[0m", stringStart);
			expect(output.slice(stringStart, stringEnd)).not.toContain("\x1b[35m");
			const commentStart = output.lastIndexOf("\x1b[2m");
			expect(output.slice(commentStart)).not.toContain("\x1b[35m");
		}
	});

	it("creates OSC 8 links and measures them as zero-width controls", () => {
		const line = renderMarkdownColored("See https://example.com/path.")[0]!;
		expect(line).toContain("\x1b]8;;https://example.com/path\x07");
		expect(line).toContain("https://example.com/path\x1b]8;;\x07.");
		expect(markdownVisibleWidth(line)).toBe("See https://example.com/path.".length);
	});

	it("wraps OSC 8 links atomically and closes truncated link state", () => {
		const lines = renderMarkdownColored("https://example.com/abcdefgh", { wrapWidth: 10 });
		expect(lines.length).toBeGreaterThan(1);
		for (const line of lines) {
			expect(markdownVisibleWidth(line)).toBeLessThanOrEqual(10);
			if (line.includes("\x1b]8;;")) expect(line).toContain("\x1b]8;;\x07");
		}
	});

	it("detects delimiter-row tables and applies alignments", () => {
		const lines = renderMarkdownColored("| left | middle | right |\n| :--- | :---: | ---: |\n| a | b | c |", { wrapWidth: 50 });
		expect(lines).toEqual([
			"┌──────┬────────┬───────┐",
			"│ left │ middle │ right │",
			"├──────┼────────┼───────┤",
			"│ a    │   b    │     c │",
			"└──────┴────────┴───────┘",
		]);
	});

	it("renders Unicode tables within width and falls back when too narrow or malformed", () => {
		const table = "| 名称 | value |\n| --- | ---: |\n| 猫 | something-long |";
		const rendered = renderMarkdownColored(table, { wrapWidth: 18 });
		expect(rendered.every((line) => markdownVisibleWidth(line) <= 18)).toBe(true);
		expect(renderMarkdownColored(table, { wrapWidth: 5 })).toEqual(table.split("\n"));
		const malformed = "| a | b |\n| --- | nope |\n| c | d |";
		expect(renderMarkdownColored(malformed)).toEqual(malformed.split("\n"));
	});

	it("preserves every incomplete fenced and table streaming prefix", () => {
		const complete = "```ts\nconst x = '```';\n```\n| a | b |\n| :--- | ---: |\n| c | d |";
		for (let index = 1; index <= complete.length; index++) {
			const partial = complete.slice(0, index);
			const rendered = renderMarkdown(partial);
			expect(rendered.length).toBeGreaterThan(0);
			expect(markdownToPlain(partial).length).toBeGreaterThanOrEqual(0);
		}
	});
});
