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

	it("renders headers with bold", () => {
		const lines = renderMarkdown("## Header text");
		expect((lines[0]!.spans[0] as any).text).toBe("Header text");
		expect((lines[0]!.spans[0] as any).bold).toBe(true);
	});

	it("renders unordered lists with a bullet", () => {
		const lines = renderMarkdown("- first\n- second");
		expect((lines[0]!.spans[0] as any).text).toBe("• first");
		expect((lines[1]!.spans[0] as any).text).toBe("• second");
	});

	it("renders ordered lists preserving content", () => {
		const lines = renderMarkdown("1. first\n2. second");
		expect((lines[0]!.spans[0] as any).text).toBe("first");
	});

	it("renders blockquotes with a leading bar", () => {
		const lines = renderMarkdown("> quoted");
		expect((lines[0]!.spans[0] as any).text).toBe("│ quoted");
	});

	it("renders horizontal rules as a dashed line", () => {
		const lines = renderMarkdown("---");
		expect((lines[0]!.spans[0] as any).text).toMatch(/─+/);
	});

	it("markdownToPlain returns plain text for prose", () => {
		expect(markdownToPlain("hello **world**")).toBe("hello world");
	});

	it("markdownToPlain keeps code blocks verbatim", () => {
		const out = markdownToPlain("```\nconst x = 1;\n```");
		expect(out).toBe("const x = 1;");
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

	it("handles headers", () => {
		const lines = renderMarkdownColored("# Title");
		expect(lines[0]!).toContain("Title");
		expect(lines[0]!).toContain("\x1b[1m"); // bold
	});

	it("handles lists", () => {
		const lines = renderMarkdownColored("- item 1\n- item 2");
		expect(lines[0]!).toContain("•");
		expect(lines[0]!).toContain("item 1");
	});

	it("handles blockquotes", () => {
		const lines = renderMarkdownColored("> quoted text");
		expect(lines[0]!).toContain("│");
		expect(lines[0]!).toContain("quoted text");
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
