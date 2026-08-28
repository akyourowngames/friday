/**
 * Tests for the Pi-style TUI. The pure helpers (wrapText, computeContentLines,
 * diffFrame, visibleWidth) are exercised directly; the Tui class is driven
 * headlessly by injecting a capturing `out` and calling handleEvent/render.
 */
import { describe, expect, it, vi } from "vitest";

const {
	wrapText,
	visibleWidth,
	computeContentLines,
	diffFrame,
	filterModels,
	visibleSelector,
	Tui,
} = await import("../src/tui.ts");

describe("visibleWidth", () => {
	it("ignores ANSI escape sequences", () => {
		expect(visibleWidth("\x1b[36myou› \x1b[0m")).toBe(5);
		expect(visibleWidth("plain")).toBe(5);
	});
});

describe("filterModels", () => {
	it("returns the whole list for an empty query", () => {
		const all = ["alpha", "beta", "gamma"];
		expect(filterModels(all, "")).toEqual(all);
		expect(filterModels(all, "   ")).toEqual(all);
	});

	it("matches case-insensitively on substring", () => {
		const all = ["claude-3-5-sonnet", "CLAUDE-OPUS", "gpt-4o"];
		expect(filterModels(all, "claude")).toEqual(["claude-3-5-sonnet", "CLAUDE-OPUS"]);
		expect(filterModels(all, "SONNET")).toEqual(["claude-3-5-sonnet"]);
	});

	it("returns an empty array when nothing matches", () => {
		expect(filterModels(["a", "b"], "zzz")).toEqual([]);
	});
});

describe("visibleSelector", () => {
	it("returns all rows when the list fits", () => {
		const { lines, cursorLine } = visibleSelector(["a", "b", "c"], 0, 10);
		expect(lines.length).toBe(3);
		expect(cursorLine).toBe(0);
		expect(lines[0]).toContain("a");
		expect(lines[0]).toContain("▶");
	});

	it("windows a large list around the cursor", () => {
		const big = Array.from({ length: 100 }, (_, i) => `m${i}`);
		const { lines } = visibleSelector(big, 50, 10);
		expect(lines.length).toBe(10);
		expect(lines.some((l) => l.includes("m50"))).toBe(true);
		// Cursor row is within the window.
		expect(lines.some((l) => l.includes("▶"))).toBe(true);
	});

	it("returns no lines for an empty list", () => {
		const { lines, cursorLine } = visibleSelector([], 0, 10);
		expect(lines).toEqual([]);
		expect(cursorLine).toBe(-1);
	});
});

describe("wrapText", () => {
	it("returns a single line when text fits", () => {
		const lines = wrapText("hello world", 20, "» ");
		expect(lines).toEqual(["» hello world\x1b[0m"]);
	});

	it("wraps long text onto continuation lines", () => {
		const lines = wrapText("the quick brown fox jumps", 10, "» ");
		expect(lines.length).toBeGreaterThan(1);
		// No wrapped line (prefix included) exceeds the width.
		for (const line of lines) {
			expect(visibleWidth(line.replace(/\x1b\[0m$/, ""))).toBeLessThanOrEqual(10);
		}
		// The concatenated visible text reconstructs the original (lines are
		// joined with spaces because trailing whitespace is trimmed at breaks).
		const joined = lines
			.map((l) => l.replace(/\x1b\[0m$/, "").replace(/^» |^ {2}/, ""))
			.join(" ")
			.replace(/\s+/g, " ")
			.trim();
		expect(joined).toBe("the quick brown fox jumps");
	});

	it("hard-splits a word longer than the line width", () => {
		const lines = wrapText("supercalifragilistic", 8, "» ");
		expect(lines.length).toBeGreaterThan(1);
		for (const line of lines) {
			expect(visibleWidth(line.replace(/\x1b\[0m$/, ""))).toBeLessThanOrEqual(8);
		}
	});

	it("preserves explicit newlines", () => {
		const lines = wrapText("line one\nline two", 40, "» ");
		expect(lines.length).toBe(2);
		expect(lines[0]).toContain("line one");
		expect(lines[1]).toContain("line two");
	});
});

describe("computeContentLines", () => {
	it("renders user, assistant, tool, and system entries with role prefixes", () => {
		const lines = computeContentLines(
			[
				{ role: "system", text: "ready" },
				{ role: "user", text: "hi" },
				{ role: "assistant", text: "hello there" },
				{ role: "tool", text: "calculator(...)" },
			],
			"",
			80,
		);
		const flat = lines.join("\n");
		expect(flat).toContain("ready");
		expect(flat).toContain("hi");
		expect(flat).toContain("hello there");
		expect(flat).toContain("calculator(...)");
	});

	it("appends the in-progress streaming text", () => {
		const lines = computeContentLines([{ role: "user", text: "hi" }], "thinking…", 80);
		expect(lines.join("\n")).toContain("thinking…");
	});
});

describe("diffFrame", () => {
	it("reports no changes for identical frames", () => {
		const d = diffFrame(["a", "b"], ["a", "b"]);
		expect(d.changed).toEqual([]);
		expect(d.cleared).toEqual([]);
	});

	it("reports changed and cleared lines", () => {
		const d = diffFrame(["a", "b", "c"], ["a", "X"]);
		expect(d.changed).toEqual([1]);
		expect(d.cleared).toEqual([2]);
	});

	it("reports new lines when the frame grows", () => {
		const d = diffFrame(["a"], ["a", "b"]);
		expect(d.changed).toEqual([1]);
		expect(d.cleared).toEqual([]);
	});

	it("treats a null previous frame as all-new", () => {
		const d = diffFrame(null, ["a", "b"]);
		expect(d.changed).toEqual([0, 1]);
	});
});

describe("Tui (headless)", () => {
	function makeTui(): { tui: Tui; writes: string[] } {
		const writes: string[] = [];
		const tui = new Tui({
			out: (t) => writes.push(t),
			getSize: () => [40, 10],
			model: "faux-1",
			provider: "faux",
			onSubmit: vi.fn(),
		});
		return { tui, writes };
	}

	const userEvent = (text: string) =>
		({ type: "message_start", message: { role: "user", content: text, timestamp: 0 } }) as any;
	const assistantUpdate = (text: string) =>
		({
			type: "message_update",
			message: { role: "assistant", content: [{ type: "text", text }], stopReason: "pending" },
		}) as any;
	const assistantEnd = (text: string) =>
		({
			type: "message_end",
			message: { role: "assistant", content: [{ type: "text", text }], stopReason: "stop" },
		}) as any;

	it("streams assistant text in place via update events, then commits to history on end", () => {
		const { tui, writes } = makeTui();
		tui.handleEvent(userEvent("hello"));
		tui.handleEvent(assistantUpdate("Hel"));
		tui.handleEvent(assistantUpdate("Hello wor"));
		tui.handleEvent(assistantUpdate("Hello world"));
		tui.handleEvent(assistantEnd("Hello world"));

		// The accumulated paints should carry the final text and the user line.
		const screen = writes.join("\n");
		expect(screen).toContain("Hello world");
		expect(screen).toContain("hello");
	});

	it("repaints from a clean frame when repaint() is called", () => {
		const { tui, writes } = makeTui();
		tui.handleEvent(userEvent("hi"));
		writes.length = 0;
		tui.repaint();
		// A clean repaint clears the screen first.
		expect(writes[0]).toContain("\x1b[2J");
	});
});

describe("Tui /model selector (headless)", () => {
	const MODELS = ["claude-3-5-sonnet", "claude-3-opus", "gpt-4o", "gemini-2.0-flash"];

	async function makeSelectorTui() {
		const writes: string[] = [];
		const selected: string[] = [];
		const tui: any = new Tui({
			out: (t: string) => writes.push(t),
			getSize: () => [40, 10],
			model: "claude-3-5-sonnet",
			provider: "claude",
			onSubmit: vi.fn(),
			onListModels: async () => MODELS,
			onSelectModel: async (id: string) => {
				selected.push(id);
				tui.setModel(id);
			},
		});
		return { tui, writes, selected };
	}

	it("opens the selector on /model, filters, and selects via Enter", async () => {
		const { tui, writes, selected } = await makeSelectorTui();

		// Open via the slash command.
		tui.inputBuffer = "/model";
		await tui.submit();
		expect(tui.mode).toBe("selector");
		// The live list is loaded and shown.
		expect(writes.join("").length).toBeGreaterThan(0);

		// Type a filter to narrow to the two claude models.
		tui.handleSelectorInput("claude");
		expect(tui.selectorFiltered.length).toBe(2);
		expect(tui.selectorFiltered).toEqual(["claude-3-5-sonnet", "claude-3-opus"]);

		// Arrow down to the second match, then confirm.
		tui.handleSelectorInput("\x1b[B");
		expect(tui.selectorCursor).toBe(1);
		tui.handleSelectorInput("\r");
		await new Promise((r) => setTimeout(r, 0));

		expect(selected).toEqual(["claude-3-opus"]);
		expect(tui.mode).toBe("chat");
		expect(tui.model).toBe("claude-3-opus");
	});

	it("cancels the selector on Esc without changing the model", async () => {
		const { tui, selected } = await makeSelectorTui();
		tui.inputBuffer = "/model";
		await tui.submit();
		expect(tui.mode).toBe("selector");
		tui.handleSelectorInput("\x1b"); // Esc cancels
		expect(tui.mode).toBe("chat");
		expect(selected).toEqual([]);
		expect(tui.model).toBe("claude-3-5-sonnet");
	});

	it("/clear wipes history", async () => {
		const { tui } = await makeSelectorTui();
		tui.handleEvent({ type: "message_start", message: { role: "user", content: "hi", timestamp: 0 } } as any);
		expect(tui.history.length).toBeGreaterThan(0);
		tui.inputBuffer = "/clear";
		await tui.submit();
		expect(tui.history.length).toBe(0);
	});
});
