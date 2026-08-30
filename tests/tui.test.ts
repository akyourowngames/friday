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
	renderHistoryEntry,
	truncateToWidth,
	diffFrame,
	filterModels,
	visibleSelector,
	Tui,
	formatToolCall,
	summarizeToolResult,
	renderToolEntry,
	renderTodos,
	buildWelcomeBox,
} = await import("../src/tui.ts");

// Lazy imports for the slash-command tests (avoids circular-import issues at
// module-eval time).
async function getSlashCmds() {
	const mod = await import("../src/slash-commands.ts");
	return mod;
}

describe("visibleWidth", () => {
	it("ignores ANSI escape sequences", () => {
		expect(visibleWidth("\x1b[36myou› \x1b[0m")).toBe(5);
		expect(visibleWidth("plain")).toBe(5);
	});

	it("treats OSC 8 hyperlinks as zero-width controls", () => {
		const link = "\x1b]8;;https://example.com\x07example\x1b]8;;\x07";
		expect(visibleWidth(link)).toBe(7);
		const clipped = truncateToWidth(link, 4);
		expect(visibleWidth(clipped)).toBe(4);
		expect(clipped).toContain("\x1b]8;;\x07");
	});

	it("counts emoji as 2 columns", () => {
		// 👋 (U+1F44B) is a surrogate pair but visually 2 columns wide.
		expect(visibleWidth("👋")).toBe(2);
		// "Hey! " (5) + 👋 (2) + " How can I help?" (16) = 23
		expect(visibleWidth("Hey! 👋 How can I help?")).toBe(23);
	});

	it("counts CJK as 2 columns", () => {
		// 你好 = 4 visible columns
		expect(visibleWidth("你好")).toBe(4);
	});

	it("ignores zero-width joiners and variation selectors", () => {
		// 👨‍👩‍👧 = man + ZWJ + woman + ZWJ + girl = 3 emoji * 2 cols = 6
		expect(visibleWidth("👨‍👩‍👧")).toBe(6);
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

	it("does not slice emoji in half when hard-splitting", () => {
		// 5 emoji = 10 visible columns, width=4 means each line carries 2
		// emoji. The output must contain the intact emoji, never a lone
		// surrogate half.
		const lines = wrapText("👋👋👋👋👋", 4, "» ");
		expect(lines.length).toBeGreaterThan(1);
		for (const line of lines) {
			// Each line (sans prefix + reset) should hold a whole number of
			// emoji — no broken surrogates.
			const stripped = line.replace(/^» |^ {2}/g, "").replace(/\x1b\[0m$/, "");
			for (const ch of stripped) {
				const cp = ch.codePointAt(0)!;
				expect(cp).not.toBe(0xfffd); // no replacement char
			}
		}
	});

	it("terminates when a wide grapheme exceeds the wrap width (regression: TUI freeze + OOM)", () => {
		// 👋 is 2 columns wide; maxText here is 1. The old sliceByWidth
		// returned head="" forever → infinite loop → frozen TUI + OOM.
		const lines = wrapText("👋 hi", 1, "» ");
		expect(lines.length).toBeGreaterThanOrEqual(2);
		const joined = lines.join("");
		expect(joined).toContain("👋");
		expect(joined).toContain("h");
		expect(joined).toContain("i");
	});

	it("hard-splits a ZWJ family emoji without hanging", () => {
		// 👨‍👩‍👧 is a single 6-column grapheme; it can never fit a 2-column
		// line, but the split must still terminate and keep it intact.
		const lines = wrapText("👨‍👩‍👧", 2, "");
		expect(lines.length).toBe(1);
		expect(lines[0]).toContain("👨‍👩‍👧");
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

describe("truncateToWidth", () => {
	it("returns the string untouched when it fits", () => {
		expect(truncateToWidth("hello", 10)).toBe("hello");
	});

	it("truncates plain text to the visible width", () => {
		const out = truncateToWidth("hello world", 5);
		expect(visibleWidth(out)).toBeLessThanOrEqual(5);
		expect(out).toContain("hello");
		expect(out).not.toContain("world");
	});

	it("never cuts an ANSI escape sequence in half and resets state", () => {
		const s = `${"\x1b[36m"}this is a colored line${"\x1b[0m"} with more text after`;
		const out = truncateToWidth(s, 10);
		// Visible width respects the cap…
		expect(visibleWidth(out)).toBeLessThanOrEqual(10);
		// …and the truncated string must not end inside an escape sequence.
		expect(out).not.toMatch(/\x1b\[[0-9]*$/);
	});

	it("appends RESET when an SGR span is cut open", () => {
		const s = `\x1b[36m${"x".repeat(40)}${"\x1b[0m"}`;
		const out = truncateToWidth(s, 10);
		expect(out.endsWith("\x1b[0m")).toBe(true);
	});

	it("handles emoji without producing lone surrogates", () => {
		const out = truncateToWidth("a👋b👋c👋d", 4);
		expect(visibleWidth(out)).toBeLessThanOrEqual(4);
		for (const ch of out) {
			const cp = ch.codePointAt(0)!;
			expect(cp).not.toBe(0xfffd);
		}
	});
});

describe("renderHistoryEntry", () => {
	it("encloses user and assistant messages in labeled boxes", () => {
		const user = renderHistoryEntry({ role: "user", text: "hi" }, 40);
		expect(user[0]).toContain("you");
		expect(user.join("\n")).toContain("hi");
		const assistant = renderHistoryEntry({ role: "assistant", text: "yo" }, 40);
		expect(assistant[0]).toContain("friday");
		expect(assistant.join("\n")).toContain("yo");
	});

	it("keeps tool and system entries as compact lines", () => {
		expect(renderHistoryEntry({ role: "tool", text: "ls" }, 40)[0]).toContain("tool");
		expect(renderHistoryEntry({ role: "system", text: "ok" }, 40)[0]).toContain("ok");
	});

	it("draws box borders with uniform width that never exceeds the wrap width", () => {
		const lines = renderHistoryEntry({ role: "assistant", text: "hello box world" }, 40);
		expect(lines[0]).toContain("╭");
		expect(lines[lines.length - 1]).toContain("╰");
		const widths = lines.map((l) => visibleWidth(l));
		expect(new Set(widths).size).toBe(1); // perfectly aligned box
		expect(widths[0]).toBeLessThanOrEqual(40);
	});

	it("leaves the streaming box open (no bottom border) when open=true via streaming", () => {
		// computeContentLines with streaming text → box without ╰ bottom.
		const lines = computeContentLines(
			[{ role: "user", text: "hi" }],
			"still typing…",
			60,
		);
		const joined = lines.join("\n");
		expect(joined).toContain("still typing…");
		expect(joined).toContain("friday");
	});

	it("wraps messages to the capped box width on wide terminals", () => {
		const text = "This sentence is deliberately longer than ninety six columns so it must wrap inside the full-width transcript box rather than leaking beyond its right border.";
		const lines = renderHistoryEntry({ role: "assistant", text }, 180);
		const widths = lines.map((line) => visibleWidth(line));
		expect(new Set(widths).size).toBe(1);
		expect(widths[0]).toBe(100); // MAX_BLOCK_INNER (96) + two borders + two gutters
		expect(lines.length).toBeGreaterThan(3);
	});

	it("wraps long command output to the capped tool box width", () => {
		const output = "command-output-".repeat(12);
		const lines = renderToolEntry({
			role: "tool",
			text: "",
			name: "bash",
			args: { command: "echo long" },
			status: "done",
			body: output,
		}, 180);
		const widths = lines.map((line) => visibleWidth(line));
		expect(new Set(widths).size).toBe(1);
		expect(widths[0]).toBe(100);
		expect(lines.length).toBeGreaterThan(3);
	});
});

describe("buildWelcomeBox", () => {
	it("uses the available wide-terminal width and keeps every row aligned", () => {
		const lines = buildWelcomeBox(180, "kilo", "stepfun/step-3.7-flash:free", 0);
		const widths = lines.map((line) => visibleWidth(line));
		expect(new Set(widths).size).toBe(1);
		expect(widths[0]).toBe(100);
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

	it("input history is empty until the user submits", () => {
		const { tui } = makeTui();
		// @ts-expect-error — peek at internals for the test
		expect(tui.inputHistory).toEqual([]);
		// @ts-expect-error
		expect(tui.inputHistoryCursor).toBe(-1);
	});

	it("submitting pushes to input history (deduplicated)", async () => {
		const onSubmit = vi.fn().mockResolvedValue(undefined);
		const tui = new Tui({
			out: () => {},
			getSize: () => [40, 10],
			model: "faux-1",
			provider: "faux",
			onSubmit,
		});
		// @ts-expect-error — peek at internals
		tui.inputBuffer = "hello";
		await tui.submit();
		// @ts-expect-error
		expect(tui.inputHistory).toEqual(["hello"]);
		// Duplicate submission is dropped.
		// @ts-expect-error
		tui.inputBuffer = "hello";
		await tui.submit();
		// @ts-expect-error
		expect(tui.inputHistory).toEqual(["hello"]);
		// A different message is appended.
		// @ts-expect-error
		tui.inputBuffer = "world";
		await tui.submit();
		// @ts-expect-error
		expect(tui.inputHistory).toEqual(["hello", "world"]);
	});

	it("up/down arrow navigates the input history", async () => {
		const onSubmit = vi.fn().mockResolvedValue(undefined);
		const tui = new Tui({
			out: () => {},
			getSize: () => [40, 10],
			model: "faux-1",
			provider: "faux",
			onSubmit,
		});
		// Submit two prompts.
		// @ts-expect-error
		tui.inputBuffer = "first";
		await tui.submit();
		// @ts-expect-error
		tui.inputBuffer = "second";
		await tui.submit();

		// Now type something else, then press ↑ to navigate to the most recent.
		// @ts-expect-error
		tui.inputBuffer = "draft";
		// @ts-expect-error
		tui.handleInput(Buffer.from("\x1b[A"));
		// @ts-expect-error
		expect(tui.inputBuffer).toBe("second");
		// Press ↑ again → "first".
		// @ts-expect-error
		tui.handleInput(Buffer.from("\x1b[A"));
		// @ts-expect-error
		expect(tui.inputBuffer).toBe("first");
		// Press ↓ twice → back to the draft.
		// @ts-expect-error
		tui.handleInput(Buffer.from("\x1b[B"));
		// @ts-expect-error
		tui.handleInput(Buffer.from("\x1b[B"));
		// @ts-expect-error
		expect(tui.inputBuffer).toBe("draft");
	});

	it("status line shows token usage after a message_update", async () => {
		const { tui, writes } = makeTui();
		tui.handleEvent({
			type: "message_update",
			message: {
				role: "assistant",
				content: [{ type: "text", text: "hi" }],
				usage: { input: 12, output: 7, totalTokens: 19 },
				stopReason: "pending",
			},
		} as any);
		// Token updates are coalesced into one frame per ~32ms — flush the
		// pending render timer before asserting.
		await new Promise((r) => setTimeout(r, 50));
		// The TUI splits the frame across multiple writes (one per changed
		// line). The status line is the second-to-last frame row, so we
		// just need to find the token usage somewhere in the cumulative output.
		const allWrites = writes.join("");
		// New status bar shows input↑ output↓ format.
		expect(allWrites).toContain("12");
		expect(allWrites).toContain("7");
	});

	it("coalesces rapid message_update events instead of painting per token", async () => {
		const { tui, writes } = makeTui();
		for (let i = 0; i < 50; i++) {
			tui.handleEvent(assistantUpdate(`chunk ${i}`));
		}
		// Before the coalesce timer fires, at most the initial frame exists;
		// after it fires, exactly one repaint happens for all 50 updates.
		await new Promise((r) => setTimeout(r, 50));
		const paints = writes.filter((w) => w.includes("chunk 49")).length;
		expect(paints).toBe(1);
	});

	it("never writes into the last terminal column (regression: scroll + status-line glue)", async () => {
		// A line whose visible width equals the full terminal width can wrap
		// when stdout.columns is stale, and a wrap at the bottom-right cell
		// scrolls the screen (missing banner top + `you›` glued after /help).
		const { tui, writes } = makeTui(); // getSize → [40, 10], so max safe = 39
		tui.handleEvent(userEvent("hi"));
		await new Promise((r) => setTimeout(r, 10));
		const payloads = writes
			.filter((w) => w.includes("\x1b[2K"))
			.map((w) => w.slice(w.indexOf("\x1b[2K") + 4));
		expect(payloads.length).toBeGreaterThan(0);
		for (const payload of payloads) {
			expect(visibleWidth(payload)).toBeLessThanOrEqual(39);
		}
		// The status bar in particular must stay below the full width.
		const status = payloads.find((p) => p.includes("faux-1"));
		expect(status).toBeDefined();
		expect(visibleWidth(status!)).toBeLessThanOrEqual(39);
	});
it("moves cursor with arrow keys and edits mid-line (readline editing)", async () => {
		const { tui } = makeTui();
		tui.handleInput(Buffer.from("hello"));
		expect((tui as any).cursorPos).toBe(5);
		// ← ← → move cursor to index 3, type 'XYZ'.
		tui.handleInput(Buffer.from("\x1b[D"));
		tui.handleInput(Buffer.from("\x1b[D"));
		tui.handleInput(Buffer.from("XYZ"));
		// Cursor was 3 → typing inserts there: "helXYZlo".
		expect(tui.inputBuffer).toBe("helXYZlo");
		// Home then kill-to-end (Ctrl+K) empties the buffer.
		tui.handleInput(Buffer.from("\x01"));
		tui.handleInput(Buffer.from("\x0b"));
		expect(tui.inputBuffer).toBe("");
	});

	it("navigates the slash-command suggestion popup with arrow keys", async () => {
		const slash = await getSlashCmds();
		slash.clearSlashCommands();
		slash.registerSlashCommand({ name: "/model", description: "Pick a model", run: () => ({ handled: true }) });
		slash.registerSlashCommand({ name: "/models", description: "List models", run: () => ({ handled: true }) });
		const { tui } = makeTui();
		// Type `/` then a partial query to open the popup.
		tui.handleInput(Buffer.from("/"));
		tui.handleInput(Buffer.from("m"));
		// Should have suggestions now (findCommands matches /model etc.).
		expect((tui as any).showSuggestions).toBe(true);
		expect(tui.inputBuffer).toBe("/m");
		// Arrow down → highlight moves / input fills the next suggestion.
		tui.handleInput(Buffer.from("\x1b[B"));
		expect((tui as any).suggestionCursor).toBe(1);
		expect(tui.inputBuffer).toBe("/models");
		slash.clearSlashCommands();
	});

	it("Ctrl+C interrupts a running operation instead of quitting", async () => {
		const interrupted: boolean[] = [];
		const tui = new Tui({
			out: () => {},
			getSize: () => [40, 10],
			model: "faux",
			provider: "faux",
			onSubmit: async () => {},
			onInterrupt: () => { interrupted.push(true); },
		});
		(tui as any).busy = true;
		tui.handleInput(Buffer.from("\x03"));
		expect(interrupted.length).toBe(1);
		expect((tui as any).busy).toBe(false);
	});

	it("reverse-search (Ctrl+R) finds and applies a history entry", async () => {
		const { tui } = makeTui();
		(tui as any).inputHistory = ["first prompt", "second prompt", "model thing"];
		tui.handleInput(Buffer.from("\x12")); // Ctrl+R
		// Type the query.
		tui.handleInput(Buffer.from("second"));
		tui.handleInput(Buffer.from("\r")); // accept
		expect((tui as any).searchActive).toBe(false);
		expect(tui.inputBuffer).toBe("second prompt");
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

	it("opens a suggestion popup with defaultModels fallback", async () => {
		const { tui } = await makeSelectorTui();
		// Don't register any providers; just open the selector with defaultModels.
		tui.defaultModels = ["gpt-4o", "gpt-4o-mini"];
		// Force onListModels to be undefined so the fallback kicks in.
		tui["onListModels"] = undefined;
		await tui.openSelector();
		expect(tui.mode).toBe("selector");
		expect(tui.selectorAll).toContain("gpt-4o");
	});
});

describe("Tui slash-command suggestions (headless)", () => {
	const cmdsPromise = getSlashCmds();

	it("does not show suggestions when input is not a slash command", () => {
		const writes: string[] = [];
		const tui: any = new Tui({
			out: (t: string) => writes.push(t),
			getSize: () => [80, 24],
			model: "gpt-4o",
			provider: "openai",
			onSubmit: vi.fn(),
			onListModels: async () => [],
		});
		tui.inputBuffer = "hello";
		tui.updateSuggestions();
		expect(tui.suggestions).toEqual([]);
		expect(tui.showSuggestions).toBe(false);
	});

	it("shows suggestions when input starts with / and matches commands", async () => {
		const { clearSlashCommands, registerSlashCommand } = await cmdsPromise;
		clearSlashCommands();
		registerSlashCommand({ name: "/help", description: "Show help", run: () => ({}) });
		registerSlashCommand({ name: "/model", description: "Pick model", run: () => ({}) });
		registerSlashCommand({ name: "/clear", description: "Clear history", run: () => ({}) });

		const writes: string[] = [];
		const tui: any = new Tui({
			out: (t: string) => writes.push(t),
			getSize: () => [80, 24],
			model: "gpt-4o",
			provider: "openai",
			onSubmit: vi.fn(),
			onListModels: async () => [],
		});
		tui.inputBuffer = "/mod";
		tui.updateSuggestions();
		expect(tui.suggestions.length).toBe(1);
		expect(tui.suggestions[0]!.name).toBe("/model");
		expect(tui.showSuggestions).toBe(true);
	});

	it("filters suggestions by prefix", async () => {
		const { clearSlashCommands, registerSlashCommand } = await cmdsPromise;
		clearSlashCommands();
		registerSlashCommand({ name: "/help", description: "Show help", run: () => ({}) });
		registerSlashCommand({ name: "/history", description: "Show history", run: () => ({}) });
		registerSlashCommand({ name: "/model", description: "Pick model", run: () => ({}) });

		const tui: any = new Tui({
			out: () => {},
			getSize: () => [80, 24],
			model: "gpt-4o",
			provider: "openai",
			onSubmit: vi.fn(),
			onListModels: async () => [],
		});
		tui.inputBuffer = "/h";
		tui.updateSuggestions();
		expect(tui.suggestions.map((s: any) => s.name)).toEqual(["/help", "/history"]);
	});

	it("Tab completes a single matching command", async () => {
		const { clearSlashCommands, registerSlashCommand } = await cmdsPromise;
		clearSlashCommands();
		registerSlashCommand({ name: "/help", description: "Show help", run: () => ({}) });
		registerSlashCommand({ name: "/history", description: "Show history", run: () => ({}) });
		registerSlashCommand({ name: "/model", description: "Pick model", run: () => ({}) });

		const tui: any = new Tui({
			out: () => {},
			getSize: () => [80, 24],
			model: "gpt-4o",
			provider: "openai",
			onSubmit: vi.fn(),
			onListModels: async () => [],
		});
		// Type "/mo" → only /model matches → Tab should complete the full name.
		tui.inputBuffer = "/mo";
		tui.updateSuggestions();
		tui.completeSlashCommand();
		expect(tui.inputBuffer).toBe("/model");
		expect(tui.showSuggestions).toBe(false);
	});

	it("Tab with multiple matches fills the common prefix", async () => {
		const { clearSlashCommands, registerSlashCommand } = await cmdsPromise;
		clearSlashCommands();
		registerSlashCommand({ name: "/help", description: "Show help", run: () => ({}) });
		registerSlashCommand({ name: "/history", description: "Show history", run: () => ({}) });
		registerSlashCommand({ name: "/model", description: "Pick model", run: () => ({}) });

		const tui: any = new Tui({
			out: () => {},
			getSize: () => [80, 24],
			model: "gpt-4o",
			provider: "openai",
			onSubmit: vi.fn(),
			onListModels: async () => [],
		});
		// "/h" matches /help and /history → common prefix is "h".
		tui.inputBuffer = "/h";
		tui.updateSuggestions();
		tui.completeSlashCommand();
		expect(tui.inputBuffer).toBe("/h");
	});

	it("renderSuggestions produces lines for each match", async () => {
		const { clearSlashCommands, registerSlashCommand } = await cmdsPromise;
		clearSlashCommands();
		registerSlashCommand({ name: "/help", description: "Show help", run: () => ({}) });
		registerSlashCommand({ name: "/model", description: "Pick model", run: () => ({}) });

		const tui: any = new Tui({
			out: () => {},
			getSize: () => [80, 24],
			model: "gpt-4o",
			provider: "openai",
			onSubmit: vi.fn(),
			onListModels: async () => [],
		});
		tui.inputBuffer = "/";
		tui.updateSuggestions();
		const lines = tui.renderSuggestions(80);
		expect(lines.length).toBe(2);
		expect(lines[0]).toContain("/help");
	});

	afterEach(() => cmdsPromise.then((c) => c.clearSlashCommands()));
});

describe("Tui tool-run rendering (the empty-box bug)", () => {
	function makeTui() {
		const writes: string[] = [];
		const tui = new Tui({
			out: (t) => writes.push(t),
			getSize: () => [80, 24],
			model: "test",
			provider: "faux",
			onSubmit: vi.fn(),
		});
		return { tui: tui as any, writes };
	}

	const toolOnlyAssistantEnd = {
		type: "message_end",
		message: {
			role: "assistant",
			content: [{ type: "toolCall", id: "c1", name: "bash", arguments: { command: "ls" } }],
			usage: { input: 0, output: 0, totalTokens: 0 },
			stopReason: "toolUse",
			timestamp: 0,
		},
	} as any;

	it("does not append an empty assistant box for a tool-only reply", () => {
		const { tui } = makeTui();
		tui.handleEvent(toolOnlyAssistantEnd);
		const roles = tui.history.map((h: any) => h.role);
		expect(roles).not.toContain("assistant");
	});

	it("shows a concise bash summary (exit code + short preview) after a tool run", () => {
		const { tui } = makeTui();
		tui.handleEvent({
			type: "tool_execution_start",
			toolCallId: "c1",
			toolName: "bash",
			args: { command: "ls" },
		} as any);
		tui.handleEvent({
			type: "tool_execution_end",
			toolCallId: "c1",
			toolName: "bash",
			isError: false,
			result: { content: [{ type: "text", text: "file-a.txt\nfile-b.ts\n[exit code 0]" }], details: { code: 0 } },
		} as any);
		const toolEntries = tui.history.filter((h: any) => h.role === "tool");
		// A single collapsed entry — not a content dump.
		expect(toolEntries.length).toBe(1);
		const text = toolEntries[0].text;
		expect(text).toContain("✓");
		expect(text).toContain("bash ls");
		expect(text).toContain("exit 0");
		expect(text).toContain("file-a.txt");
	});

	it("does NOT dump file contents into the chat after a read", () => {
		const { tui } = makeTui();
		tui.handleEvent({
			type: "tool_execution_start",
			toolCallId: "c1",
			toolName: "read",
			args: { path: "src/tui.ts" },
		} as any);
		tui.handleEvent({
			type: "tool_execution_end",
			toolCallId: "c1",
			toolName: "read",
			isError: false,
			result: {
				content: [{ type: "text", text: "     1│ import ...\n     2│ export ..." }],
				details: { totalLines: 1800, shown: 1800 },
			},
		} as any);
		const toolEntries = tui.history.filter((h: any) => h.role === "tool");
		expect(toolEntries.length).toBe(1);
		const text = toolEntries[0].text;
		expect(text).toContain("read src/tui.ts");
		expect(text).toContain("1800 lines");
		expect(text).not.toContain("import");
		expect(text).not.toContain("export");
	});

	it("summarizes glob / grep match counts instead of listing every match", () => {
		const { tui } = makeTui();
		tui.handleEvent({
			type: "tool_execution_end",
			toolCallId: "c1",
			toolName: "grep",
			isError: false,
			result: { content: [{ type: "text", text: "a.ts:1: x\nb.ts:2: y" }], details: { matches: 47 } },
		} as any);
		const text = tui.history.filter((h: any) => h.role === "tool")[0].text;
		expect(text).toContain("47 matches");
		expect(text).not.toContain("a.ts:1");
	});

	it("surfaces errorMessage as a system line instead of an empty box", () => {
		const { tui } = makeTui();
		tui.handleEvent({
			type: "message_end",
			message: {
				role: "assistant",
				content: [],
				usage: { input: 0, output: 0, totalTokens: 0 },
				stopReason: "error",
				errorMessage: "gateway 500",
				timestamp: 0,
			},
		} as any);
		const sys = tui.history.filter((h: any) => h.role === "system");
		expect(sys.length).toBe(1);
		expect(sys[0].text).toContain("[error] gateway 500");
	});

	it("flags a genuinely empty reply instead of a silent box", () => {
		const { tui } = makeTui();
		tui.handleEvent({
			type: "message_end",
			message: {
				role: "assistant",
				content: [],
				usage: { input: 0, output: 0, totalTokens: 0 },
				stopReason: "stop",
				timestamp: 0,
			},
		} as any);
		const sys = tui.history.filter((h: any) => h.role === "system");
		expect(sys.length).toBe(1);
		expect(sys[0].text).toContain("(empty response)");
	});
});

describe("structured tool rendering and pinned todos", () => {
	it("renders running, done, and error tools in width-safe boxes", () => {
		for (const status of ["running", "done", "error"] as const) {
			const lines = renderToolEntry({ role: "tool", text: "", toolCallId: "x", name: "bash", args: { command: "echo hello" }, status, result: { content: [{ type: "text", text: "ok" }], details: { code: status === "error" ? 1 : 0 } } }, 30);
			expect(lines[0]).toContain(status === "running" ? "●" : status === "done" ? "✓" : "✗");
			for (const line of lines) expect(visibleWidth(line)).toBeLessThanOrEqual(30);
		}
	});

	it("keeps plain legacy tool entries compatible", () => {
		expect(renderToolEntry({ role: "tool", text: "legacy output" }, 40).join("\n")).toContain("legacy output");
	});

	it("collapses tool bodies to 12 lines and expands fully", () => {
		const body = Array.from({ length: 20 }, (_, i) => `line-${i}`).join("\n");
		const base = { role: "tool", text: "", name: "bash", args: {}, status: "running", body } as const;
		const collapsed = renderToolEntry(base, 80).join("\n");
		expect(collapsed).toContain("line-11");
		expect(collapsed).not.toContain("line-12");
		expect(renderToolEntry({ ...base, expanded: true }, 80).join("\n")).toContain("line-19");
	});

	it("renders edit details as red and green line diffs", () => {
		const lines = renderToolEntry({ role: "tool", text: "", name: "edit", args: { path: "a.ts" }, status: "done", result: { content: [], details: { path: "a.ts", oldText: "old", newText: "new" } } }, 60).join("\n");
		expect(lines).toContain("- old");
		expect(lines).toContain("+ new");
		expect(lines).toContain("\x1b[31m");
		expect(lines).toContain("\x1b[32m");
	});

	it("renders bounded todos without including them in scrollback", () => {
		const todos = Array.from({ length: 5 }, (_, i) => ({ text: `todo-${i}`, status: i === 0 ? "completed" : "pending" }));
		const lines = renderTodos(todos, 20, 3);
		expect(lines).toHaveLength(3);
		expect(lines.join("\n")).toContain("todo-0");
		expect(lines[2]).toContain("more todos");
		for (const line of lines) expect(visibleWidth(line)).toBeLessThanOrEqual(20);
	});

	it("matches interleaved tool completion and progress by ID", async () => {
		const tui: any = new Tui({ out: () => {}, getSize: () => [80, 24], model: "m", provider: "p", onSubmit: vi.fn(), streamDebounceMs: 0 });
		tui.handleEvent({ type: "tool_execution_start", toolCallId: "a", toolName: "bash", args: { command: "a" } });
		tui.handleEvent({ type: "tool_execution_start", toolCallId: "b", toolName: "bash", args: { command: "b" } });
		tui.handleEvent({
			type: "tool_execution_progress",
			toolCallId: "a",
			toolName: "bash",
			progress: { content: [{ type: "text", text: "chunk-a" }] },
		});
		tui.handleEvent({ type: "tool_execution_end", toolCallId: "a", toolName: "bash", result: { content: [], details: { code: 0 } }, isError: false });
		expect(tui.history[0].status).toBe("done");
		expect(tui.history[0].body).toBe("chunk-a");
		expect(tui.history[1].status).toBe("running");
	});

	it("Ctrl+O toggles only the most recent structured tool", () => {
		const tui: any = new Tui({ out: () => {}, getSize: () => [80, 24], model: "m", provider: "p", onSubmit: vi.fn() });
		tui.handleEvent({ type: "tool_execution_start", toolCallId: "a", toolName: "bash", args: {} });
		tui.handleEvent({ type: "tool_execution_start", toolCallId: "b", toolName: "bash", args: {} });
		tui.handleInput(Buffer.from("\x0f"));
		expect(tui.history[0].expanded).toBeUndefined();
		expect(tui.history[1].expanded).toBe(true);
	});

	it("serializes confirmations, blocks normal input, and supports yes/no keys", async () => {
		const input = { isTTY: true } as any;
		const tui: any = new Tui({ input, out: () => {}, getSize: () => [80, 24], model: "m", provider: "p", onSubmit: vi.fn() });
		const first = tui.confirm("first?");
		const second = tui.confirm("second?");
		tui.handleInput(Buffer.from("ignored"));
		expect(tui.inputBuffer).toBe("");
		tui.handleInput(Buffer.from("y"));
		expect(await first).toBe(true);
		expect(tui.activeConfirmation.prompt).toBe("second?");
		tui.handleInput(Buffer.from("\x1b"));
		expect(await second).toBe(false);
	});

	it("rejects confirmation safely on non-TTY input", async () => {
		const tui = new Tui({ input: { isTTY: false } as any, out: () => {}, getSize: () => [80, 24], model: "m", provider: "p", onSubmit: vi.fn() });
		expect(await tui.confirm("danger?")).toBe(false);
	});

	it("submits slash follow-ups through the normal busy path once", async () => {
		const onSubmit = vi.fn().mockResolvedValue(undefined);
		const onSlashCommand = vi.fn().mockResolvedValue({ handled: true, submitFollowUp: "follow up" });
		const tui: any = new Tui({ out: () => {}, getSize: () => [80, 24], model: "m", provider: "p", onSubmit, onSlashCommand });
		tui.inputBuffer = "/go";
		await tui.submit();
		expect(onSlashCommand).toHaveBeenCalledTimes(1);
		expect(onSubmit).toHaveBeenCalledWith("follow up");
		expect(tui.busy).toBe(false);
	});

	it("uses setting hooks and reconstructs tool calls from conversation", () => {
		const setSetting = vi.fn();
		const tui: any = new Tui({ out: () => {}, getSize: () => [80, 24], model: "m", provider: "p", onSubmit: vi.fn(), getSetting: (key) => key === "x" ? 7 : undefined, setSetting });
		expect(tui.getSetting("x")).toBe(7);
		tui.setSetting("x", 8);
		expect(setSetting).toHaveBeenCalledWith("x", 8);
		tui.loadConversation([
			{ role: "assistant", content: [{ type: "toolCall", id: "c", name: "read", arguments: { path: "a" } }] },
			{ role: "toolResult", toolCallId: "c", toolName: "read", content: [], details: { totalLines: 3 }, isError: false },
		]);
		expect(tui.history[0]).toMatchObject({ toolCallId: "c", name: "read", status: "done" });
	});
});

describe("formatToolCall / summarizeToolResult", () => {
	it("formatToolCall shows the command for bash, path for file tools, query for websearch", () => {
		expect(formatToolCall("bash", { command: "date /t" })).toBe("bash date /t");
		expect(formatToolCall("read", { path: "src/tui.ts", startLine: 1 })).toBe("read src/tui.ts");
		expect(formatToolCall("websearch", { query: "node.js news" })).toBe("websearch node.js news");
		expect(formatToolCall("glob", { pattern: "**/*.ts" })).toBe("glob **/*.ts");
	});

	it("formatToolCall clips long arguments to one line", () => {
		const long = "x".repeat(200);
		const out = formatToolCall("bash", { command: long });
		expect(out.length).toBeLessThanOrEqual(90);
		expect(out.endsWith("…")).toBe(true);
	});

	it("formatToolCall collapses multi-line commands", () => {
		expect(formatToolCall("bash", { command: "echo a\necho b" })).toBe("bash echo a ⏎ echo b");
	});

	it("summarizeToolResult reports bash exit codes with a capped output preview", () => {
		const many = Array.from({ length: 30 }, (_, i) => `line-${i}`).join("\n");
		const out = summarizeToolResult("bash", {
			content: [{ type: "text", text: `${many}\n[exit code 0]` }],
			details: { code: 0 },
		}, false);
		expect(out).toContain("exit 0");
		expect(out).toContain("line-0");
		expect(out).not.toContain("line-5");
	});

	it("summarizeToolResult reports timeouts", () => {
		const out = summarizeToolResult("bash", {
			content: [{ type: "text", text: "partial" }],
			details: { code: null, timedOut: true },
		}, false);
		expect(out).toContain("timed out");
	});

	it("summarizeToolResult reports line/match/result counts", () => {
		expect(summarizeToolResult("read", { content: [], details: { totalLines: 12 } }, false)).toBe("12 lines");
		expect(summarizeToolResult("grep", { content: [], details: { matches: 3 } }, false)).toBe("3 matches");
		expect(summarizeToolResult("glob", { content: [], details: { matches: 1 } }, false)).toBe("1 match");
		expect(summarizeToolResult("websearch", { content: [], details: { results: 5, source: "duckduckgo" } }, false)).toBe(
			"5 results (duckduckgo)",
		);
		expect(summarizeToolResult("write", { content: [], details: { bytes: 42 } }, false)).toBe("wrote 42 bytes");
		expect(summarizeToolResult("edit", { content: [], details: {} }, false)).toBe("edited");
	});

	it("summarizeToolResult surfaces errors briefly", () => {
		const out = summarizeToolResult(
			"bash",
			{ content: [{ type: "text", text: `Error: ${"boom ".repeat(60)}` }] },
			true,
		);
		expect(out.startsWith("error: ")).toBe(true);
		expect(out.length).toBeLessThanOrEqual(180);
	});
});
