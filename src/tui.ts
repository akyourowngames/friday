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
}

export class Tui {
	private write: (text: string) => void;
	private input: NodeJS.ReadStream;
	private getSize: () => [number, number];
	private model: string;
	private provider: string;
	private onSubmit: (text: string) => Promise<void> | void;
	private onQuit?: () => void;
	private showThinking: boolean;

	private history: TuiHistoryEntry[] = [];
	private streamingText = "";
	private inputBuffer = "";
	private status = "";
	private busy = false;

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

	private writeFrame(frame: string[], _cols: number, rows: number): void {
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

		// Position the hardware cursor at the end of the input line.
		const inputRow = Math.min(frame.length, rows);
		const col = 1 + USER_PREFIX_VW + visibleWidth(this.inputBuffer);
		this.write(`${ESC}[${inputRow};${col}H${ESC}[?25h`);

		this.prevFrame = frame;
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
