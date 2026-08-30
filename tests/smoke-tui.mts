/**
 * Headless smoke test for the TUI rendering fixes (see todo.txt).
 * Verifies: banner box alignment, frame-line clamping, streaming cursor,
 * and that the frame never overflows the terminal.
 */
import { Tui, visibleWidth, computeContentLines, STREAM_CURSOR_FRAMES } from "../src/tui.ts";

const COLS = 80;
const ROWS = 24;
const writes: string[] = [];

const tui = new Tui({
	out: (t) => writes.push(t),
	getSize: () => [COLS, ROWS],
	model: "tencent/hy3:free",
	provider: "kilo",
	onSubmit: async () => {},
	onListModels: async () => ["tencent/hy3:free", "other/model:1b"],
});

// Draw the welcome banner by simulating run()'s banner block headlessly:
// instead of calling run() (needs a TTY), feed a user turn + long streaming
// assistant reply with emoji and verify frame invariants.
const ev = (type: string, extra: Record<string, unknown> = {}) =>
	({ type, ...extra }) as any;

tui.handleEvent(ev("message_start", { message: { role: "user", content: "hi 👋", timestamp: 0 } }));
tui.handleEvent(ev("message_update", {
	message: {
		role: "assistant",
		content: [{ type: "text", text: "Hello! 👋 This is a fairly long streaming reply with emoji 🚀 and CJK 你好 text that must wrap and clamp correctly at every width." }],
		usage: { input: 1234, output: 567, totalTokens: 1801 },
		stopReason: "pending",
	},
}));

// Token renders are coalesced (~32ms) — wait for the pending frame to paint.
await new Promise((r) => setTimeout(r, 60));

// Extract the last frame: writes are ANSI cursor-addressed line updates.
// Reconstruct the screen from the final full repaint.
const all = writes.join("");
// Every written line payload after a cursor-position must be <= COLS wide.
// Extract frame line payloads: writeFrame emits `ESC[<row>;1H ESC[2K <line>`.
// Splitting the joined writes on `ESC[` leaves segments like `2K<line>`.
console.log(`write calls: ${writes.length}, total bytes: ${all.length}`);
// Each frame-line write looks like `ESC[<row>;1H ESC[2K <payload>`. Payloads
// contain their own escapes, so slice per write rather than splitting.
const linePayloads = writes
	.filter((w) => w.includes("\x1b[2K"))
	.map((w) => w.slice(w.indexOf("\x1b[2K") + 4))
	.filter((p) => p.length > 0);

let bad = 0;
if (linePayloads.length < 3) {
	console.error("FAIL: expected several frame-line writes, got " + linePayloads.length);
	process.exit(1);
}
for (const payload of linePayloads) {
	const stripped = payload.replace(/\x1b\[[0-9;]*[A-Za-z]/g, "");
	if (visibleWidth(stripped) > COLS) bad++;
}
console.log(`line payloads: ${linePayloads.length}, over-width: ${bad}`);
if (bad > 0) {
	console.error("FAIL: found lines wider than the terminal (status-bar wrap bug)");
	process.exit(1);
}

// Streaming content + cursor block must be on screen.
if (!all.includes("streaming reply")) { console.error("FAIL: streaming text missing"); process.exit(1); }
// The streaming caret is animated, so accept any frame of the cycle rather
// than pinning one glyph (which would make this flaky ~75% of the time).
if (!STREAM_CURSOR_FRAMES.some((f) => all.includes(f))) { console.error("FAIL: streaming cursor missing"); process.exit(1); }
if (!all.includes("1,234↑") || !all.includes("567↓")) { console.error("FAIL: token usage missing from status bar"); process.exit(1); }

// Banner: box lines must all share the same visible width and fit the terminal.
import { buildWelcomeBox } from "../src/tui.ts";
for (const cols of [80, 50, 46, 40, 30, 20]) {
	const box = buildWelcomeBox(cols, "kilo", "tencent/hy3:free");
	const boxed = box[0]!.includes("┌");
	const widths = box.map((l) => visibleWidth(l.replace(/\x1b\[[0-9;]*m/g, "")));
	if (boxed) {
		const unique = new Set(widths);
		if (unique.size !== 1) {
			console.error(`FAIL: banner misaligned at cols=${cols}: widths=${widths.join(",")}`);
			process.exit(1);
		}
		if (Math.max(...widths) > cols) {
			console.error(`FAIL: banner wider than terminal at cols=${cols}: ${Math.max(...widths)}`);
			process.exit(1);
		}
	}
	// Non-boxed fallback lines are re-wrapped by wrapText at render time.
}

console.log("SMOKE OK: banner aligned at all widths, no over-width lines, streaming cursor + usage present");

// Visual preview of the new boxed message UI (ANSI stripped).
const preview = computeContentLines(
	[
		{ role: "user", text: "what can yu do" },
		{
			role: "assistant",
			text: "I can do a bunch of stuff. Here's a quick rundown:\n\n🛠️  Coding & Dev\n• Write, read, and edit files\n• Run shell commands / scripts",
		},
		{ role: "user", text: "cool hehe" },
	],
	"And streaming continues with a block cur",
	76,
);
console.log("\n----- PREVIEW (76 cols, ANSI stripped) -----");
for (const line of preview) {
	console.log(line.replace(/\x1b\[[0-9;]*m/g, ""));
}
console.log("----- END PREVIEW -----");
