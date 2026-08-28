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

const ESC = "\x1b";
const RESET = `${ESC}[0m`;
const DIM = `${ESC}[2m`;
const CYAN = `${ESC}[36m`;
const MAGENTA = `${ESC}[35m`;
const YELLOW = `${ESC}[33m`;
const REVERSE = `${ESC}[7m`;

const USER_PREFIX = `${CYAN}you› ${RESET}`;
const USER_PREFIX_VW = 5; // visible width of "you› "
const ASSISTANT_PREFIX = `${MAGENTA}friday› ${RESET}`;
const ASSISTANT_PREFIX_VW = 7;
const TOOL_PREFIX = `${YELLOW}tool ${RESET}`;
const TOOL_PREFIX_VW = 5;

export type TuiRole = "user" | "assistant" | "tool" | "system";

export interface TuiHistoryEntry {
	role: TuiRole;
	text: string;
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
			const maxText = width - visibleWidth(linePrefix);
			if (line.length === 0 && !isSpace && visibleWidth(tok) > maxText) {
				// First token on an empty line but still too long → hard-split now.
				let rem = tok;
				while (visibleWidth(rem) > maxText) {
					out.push(`${linePrefix}${rem.slice(0, maxText)}${reset}`);
					rem = rem.slice(maxText);
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
					out.push(`${linePrefix}${rem.slice(0, maxText)}${reset}`);
					rem = rem.slice(maxText);
					linePrefix = contPrefix;
				}
				line = rem;
			}
		}
		if (line.length > 0) out.push(`${linePrefix}${line.trimEnd()}${reset}`);
	}
	return out;
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
): { lines: string[]; cursorLine: number } {
	const count = list.length;
	if (count === 0) return { lines: [], cursorLine: -1 };
	const start = Math.max(0, Math.min(cursor - Math.floor(rows / 2), Math.max(0, count - rows)));
	const end = Math.min(count, start + Math.max(1, rows));
	const lines: string[] = [];
	let cursorLine = -1;
	for (let i = start; i < end; i++) {
		const isCursor = i === cursor;
		const marker = isCursor ? `${REVERSE}▶ ${RESET}` : "  ";
		const text = (list[i] ?? "").slice(0, Math.max(0, 200));
		lines.push(`${marker}${text}${RESET}`);
		if (isCursor) cursorLine = lines.length - 1;
	}
	return { lines, cursorLine };
}

/** Visible (printed) width of a string, ignoring ANSI escape sequences. */
export function visibleWidth(s: string): number {
	let width = 0;
	let inEscape = false;
	for (let i = 0; i < s.length; i++) {
		const ch = s.charCodeAt(i);
		if (inEscape) {
			if (ch === 0x6d /* 'm' */) inEscape = false;
			continue;
		}
		if (ch === 0x1b /* ESC */) {
			inEscape = true;
			continue;
		}
		width += 1;
	}
	return width;
}
/** Compute the scrollback + streaming content lines (everything above the
 *  status/input reserved rows). Pure and testable. */
export function computeContentLines(
	history: TuiHistoryEntry[],
	streamingText: string,
	width: number,
): string[] {
	const lines: string[] = [];
	for (const entry of history) {
		switch (entry.role) {
			case "user":
				lines.push(...wrapText(entry.text, width, USER_PREFIX));
				break;
			case "assistant":
				lines.push(...wrapText(entry.text, width, ASSISTANT_PREFIX));
				break;
			case "tool":
				lines.push(...wrapText(entry.text, width, TOOL_PREFIX));
				break;
			case "system":
				lines.push(...wrapText(entry.text, width, DIM));
				break;
		}
	}
	if (streamingText.length > 0) {
		lines.push(...wrapText(streamingText, width, ASSISTANT_PREFIX));
	}
	return lines;
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

export interface TuiOptions {
	out?: (text: string) => void;
	input?: NodeJS.ReadStream;
	getSize?: () => [number, number];
	model: string;
	provider: string;
	onSubmit: (text: string) => Promise<void> | void;
	onQuit?: () => void;
	showThinking?: boolean;
	/** Returns the live model list (CLI wires this to the gateway). */
	onListModels?: () => Promise<string[]>;
	/** Called when the user picks a model in the `/model` selector. */
	onSelectModel?: (id: string) => Promise<void> | void;
	/** Called by the `/clear` command (optional; history is always wiped). */
	onClear?: () => void;
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
	private showThinking: boolean;

	private history: TuiHistoryEntry[] = [];
	private streamingText = "";
	private inputBuffer = "";
	private status = "";
	private busy = false;

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

	private resolveRun?: () => void;
	private onData = (chunk: Buffer) => this.handleInput(chunk);

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
		this.showThinking = options.showThinking ?? false;
	}

	/** Enter the TUI. Resolves when the user quits. `initialPrompt`, if given,
	 *  is submitted as the first message once the screen is up. */
	async run(initialPrompt?: string): Promise<void> {
		this.write(`${ESC}[?1049h`); // enter alt screen
		this.write(`${ESC}[?25l`); // hide cursor while drawing
		this.appendHistory({
			role: "system",
			text: `friday-ng • ${this.provider}/${this.model} • type a message, Ctrl+C or Esc to quit`,
		});
		this.render();

		this.input.setRawMode?.(true);
		this.input.resume();
		this.input.on("data", this.onData);

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
					this.appendHistory({ role: "user", text: this.userText(event.message) });
					this.userScroll = 0;
				} else if (event.message.role === "assistant") {
					this.streamingText = "";
					this.userScroll = 0;
				}
				break;

			case "message_update":
				if (event.message.role === "assistant") {
					this.streamingText = this.assistantText(event.message);
					this.userScroll = 0;
				}
				break;

			case "message_end":
				if (event.message.role === "assistant") {
					this.appendHistory({ role: "assistant", text: this.assistantText(event.message) });
					this.streamingText = "";
				}
				break;

			case "tool_execution_start":
				this.appendHistory({
					role: "tool",
					text: `${event.toolName}(${safeStringify(event.args)})`,
				});
				break;

			case "tool_execution_end":
				this.appendHistory({
					role: "tool",
					text: `→ ${event.isError ? "error" : "ok"}`,
				});
				break;

			default:
				break;
		}
		this.render();
	}

	/** Force a repaint (also clears the cached frame so the whole screen redraws). */
	repaint(): void {
		this.prevFrame = null;
		this.render();
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
			if (c.type === "text") parts.push(c.text);
			else if (c.type === "thinking" && this.showThinking) parts.push(`[thinking: ${c.thinking}]`);
		}
		return parts.join("");
	}

	private appendHistory(entry: TuiHistoryEntry): void {
		this.history.push(entry);
	}

	private handleInput(chunk: Buffer): void {
		const s = chunk.toString();
		if (this.mode === "selector") {
			this.handleSelectorInput(s);
			return;
		}
		if (s === "\x03" || s === "\x1b") {
			this.quit();
			return;
		}
		if (s === "\r" || s === "\n") {
			void this.submit();
			return;
		}
		if (s === "\x7f" || s === "\b") {
			this.inputBuffer = this.inputBuffer.slice(0, -1);
			this.render();
			return;
		}
		if (s === "\x0c") {
			this.repaint();
			return;
		}
		if (s.startsWith("\x1b[")) {
			if (s === "\x1b[A") this.scroll(-1);
			else if (s === "\x1b[B") this.scroll(1);
			return;
		}
		if (s >= " " && !s.startsWith("\x1b")) {
			this.inputBuffer += s;
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
		if (!text || this.busy) {
			this.inputBuffer = "";
			this.render();
			return;
		}
		this.inputBuffer = "";

		// Slash commands (only when not busy). Unknown `/x` falls through as a
		// normal message below.
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

		this.busy = true;
		this.status = "thinking…";
		this.render();

		try {
			await this.onSubmit(text);
		} catch (err) {
			this.appendHistory({
				role: "system",
				text: `[error] ${err instanceof Error ? err.message : String(err)}`,
			});
		} finally {
			this.busy = false;
			this.status = "";
			this.render();
		}
	}

	private quit(): void {
		this.input.removeListener("data", this.onData);
		this.input.setRawMode?.(false);
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
		const [cols, rows] = this.getSize();
		const reserved = 2; // status line + input line
		const usable = Math.max(1, rows - reserved);

		const content = computeContentLines(this.history, this.streamingText, cols);
		const autoOffset = Math.max(0, content.length - usable);
		const offset = Math.min(autoOffset, autoOffset + this.userScroll);
		const visible = content.slice(offset);

		const statusLine = `${DIM}${this.status || `${this.provider}/${this.model}`} • ${this.history.length} msg${RESET}`;
		const inputLine = `${USER_PREFIX}${this.inputBuffer}${RESET}`;
		const frame = [...visible, statusLine, inputLine];

		this.writeFrame(frame, cols, rows);
	}

	/** Draw the `/model` selector: filter box on top, windowed list, hint at bottom. */
	private renderSelector(): void {
		const [cols, rows] = this.getSize();
		const hintLine = `${DIM}↑↓ move • Enter select • Esc cancel${RESET}`;
		const filterLine = `${YELLOW}/model › ${RESET}${this.selectorFilter}${RESET}`;
		const usable = Math.max(1, rows - 2); // minus filter + hint

		let listLines: string[];
		if (this.selectorLoading) {
			listLines = [`${DIM}loading models…${RESET}`];
		} else if (this.selectorFiltered.length === 0) {
			listLines = [`${DIM}(no models match "${this.selectorFilter}")${RESET}`];
		} else {
			listLines = visibleSelector(this.selectorFiltered, this.selectorCursor, usable).lines;
		}

		const frame = [filterLine, ...listLines, hintLine];
		// Cursor sits at the end of the filter line (row 1).
		const cursorCol = 1 + visibleWidth("/model › ") + visibleWidth(this.selectorFilter);
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
		const row = cursorRow ?? Math.min(frame.length, rows);
		const col = cursorCol ?? 1 + USER_PREFIX_VW + visibleWidth(this.inputBuffer);
		this.write(`${ESC}[${row};${col}H${ESC}[?25h`);

		this.prevFrame = frame;
	}

	/** Update the displayed model (called after a `/model` selection). */
	setModel(modelId: string): void {
		this.model = modelId;
		if (this.history[0]?.role === "system") {
			this.history[0].text = `friday-ng • ${this.provider}/${this.model} • type a message, Ctrl+C or Esc to quit`;
		}
		this.render();
	}
}

function safeStringify(value: unknown): string {
	try {
		const s = JSON.stringify(value);
		return s && s.length > 120 ? `${s.slice(0, 120)}…` : s ?? "";
	} catch {
		return "";
	}
}
