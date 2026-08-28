/**
 * Console renderer for streaming agent events.
 *
 * Renders the AgentEvent stream to stdout. Assistant text is painted as a
 * *block*: on every update we move the cursor back up to the first line we
 * wrote and clear downward, then rewrite the full (wrapped) block in place.
 * This avoids the classic "carriage-return-only" bug where wrapping text gets
 * duplicated because a single `\r` can't rewind past a wrapped line.
 *
 * For a full terminal UI (scrollback, input line, alt-screen), use the TUI
 * in `./tui.ts` via `friday-ng -i`.
 */
import type { AgentEvent, AssistantMessage, AgentMessage } from "./types.ts";

const ANSI_HIDE_CURSOR = "\x1b[?25l";
const ANSI_SHOW_CURSOR = "\x1b[?25h";

export interface ConsoleRendererOptions {
	out?: (text: string) => void;
	showThinking?: boolean;
}

export class ConsoleRenderer {
	private output: (text: string) => void;
	private showThinking: boolean;
	private currentAssistantText: string = "";
	/** Number of terminal rows the last painted assistant block occupied. */
	private assistantRowCount = 0;

	constructor(options: ConsoleRendererOptions = {}) {
		this.output = options.out ?? ((text) => process.stdout.write(text));
		this.showThinking = options.showThinking ?? false;
	}

	render(event: AgentEvent): void {
		switch (event.type) {
			case "agent_start":
				this.currentAssistantText = "";
				this.assistantRowCount = 0;
				break;

			case "message_start":
				if (event.message.role === "user") {
					this.output(`\nYou: ${this.userMessageText(event.message)}\n`);
				} else if (event.message.role === "assistant") {
					// New assistant turn — forget the previous block so the next
					// paint starts fresh below the already-finalized text.
					this.currentAssistantText = "";
					this.assistantRowCount = 0;
				}
				break;

			case "message_update":
				if (event.message.role === "assistant") {
					this.paintAssistant(this.assistantText(event.message));
				}
				break;

			case "message_end":
				if (event.message.role === "assistant") {
					// Make sure the final text is on screen, then drop to a fresh line.
					this.paintAssistant(this.assistantText(event.message));
					this.output("\n");
				} else if (event.message.role === "toolResult") {
					this.output(`\n[Tool result for ${event.message.toolName}]\n`);
				}
				break;

			case "tool_execution_start":
				this.output(`\n≻ ${event.toolName}(${JSON.stringify(event.args)})\n`);
				break;

			case "tool_execution_end":
				this.output(`[${event.isError ? "Error" : "Result"}: ${this.toolResultText(event.result)}]\n`);
				break;

			case "turn_end":
				break;

			case "agent_end":
				this.output("Done.\n");
				break;
		}
	}

	private userMessageText(message: AgentMessage): string {
		if (typeof message.content === "string") {
			return message.content;
		}
		return message.content
			.map((c) => (c.type === "text" || c.type === "image" ? (c.type === "text" ? c.text : `[${c.mimeType}]`) : "[unknown]"))
			.join("\n");
	}

	private toolResultText(result: { content: any[] }): string {
		return result.content
			.map((c: any) => (c.type === "text" ? c.text : `[image:${c.mimeType}]`))
			.join("\n")
			.slice(0, 200);
	}

	private assistantText(message: AssistantMessage): string {
		const parts: string[] = [];
		for (const c of message.content) {
			if (c.type === "text") parts.push(c.text);
			else if (c.type === "thinking" && this.showThinking) parts.push(`(thinking) ${c.thinking}`);
		}
		return parts.join(this.showThinking ? "\n" : "");
	}

	/** Paint the assistant block in place: clear the previously painted rows,
	 *  then write the wrapped text. No-op when nothing changed. */
	private paintAssistant(text: string): void {
		if (text === this.currentAssistantText) return;

		// Clear the previous block (if any) by rewinding to its first row.
		if (this.assistantRowCount > 0) {
			const moves = Math.max(0, this.assistantRowCount - 1);
			// \x1b[<moves>A : up to the first row of the block
			// \r              : column 1
			// \x1b[J          : clear from cursor to end of screen
			this.output(`${"\x1b["}${moves}A\r\x1b[J`);
		}

		const width = (process.stdout.columns as number) || 80;
		const lines = wrapToWidth(text, width);
		this.output(`${ANSI_HIDE_CURSOR}${lines.join("\n")}${ANSI_SHOW_CURSOR}`);
		this.currentAssistantText = text;
		this.assistantRowCount = lines.length;
	}
}

/** Split `text` into terminal rows of at most `width` columns, honoring
 *  explicit newlines. Used to know how many rows to clear on the next paint. */
export function wrapToWidth(text: string, width: number): string[] {
	const out: string[] = [];
	for (const para of text.split("\n")) {
		if (para.length === 0) {
			out.push("");
			continue;
		}
		let line = "";
		const tokens = para.split(/(\s+)/).filter((t) => t.length > 0);
		for (const tok of tokens) {
			if (line.length === 0) {
				line = tok;
				continue;
			}
			if (line.length + tok.length <= width) {
				line += tok;
			} else {
				out.push(line);
				line = tok;
			}
		}
		out.push(line);
	}
	return out;
}

/** Convenience function: subscribe a renderer to an event emitter. */
export function attachConsoleRenderer(
	agent: { on(event: "event", listener: (event: AgentEvent) => void): void },
	options: ConsoleRendererOptions = {},
): ConsoleRenderer {
	const renderer = new ConsoleRenderer(options);
	agent.on("event", (event: AgentEvent) => renderer.render(event));
	return renderer;
}
