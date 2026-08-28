/**
 * Console renderer for streaming agent events.
 *
 * Renders the AgentEvent stream to stdout, using ANSI escape codes for
 * in-place text updates (so tokens appear "instantly" as the LLM streams).
 *
 * This is a lightweight console-based renderer — for a full terminal UI,
 * use the pi-tui package from the Pi harness.
 */
import type { AgentEvent, AssistantMessage, AgentMessage } from "./types.ts";

const ANSI_CLEAR_LINE = "\x1b[1K\r";
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
	private currentToolId: string | null = null;
	private currentToolArgs: string = "";

	constructor(options: ConsoleRendererOptions = {}) {
		this.output = options.out ?? ((text) => process.stdout.write(text));
		this.showThinking = options.showThinking ?? false;
	}

	render(event: AgentEvent): void {
		switch (event.type) {
			case "agent_start":
				this.currentAssistantText = "";
				this.currentToolId = null;
				this.currentToolArgs = "";
				break;

			case "message_start":
				if (event.message.role === "user") {
					this.output(`\n${ANSI_CLEAR_LINE}You: ${this.userMessageText(event.message)}\n`);
				}
				break;

			case "message_update": {
				if (event.message.role === "assistant") {
					this.renderStreamingAssistant(event.message);
				}
				break;
			}

			case "message_end":
				if (event.message.role === "assistant") {
					// Ensure final text is flushed
					this.flushStreamingAssistant(event.message);
					this.output("\n");
				} else if (event.message.role === "toolResult") {
					this.output(`\n${ANSI_CLEAR_LINE}[Tool result for ${event.message.toolName}]\n`);
				}
				break;

			case "tool_execution_start":
				this.output(`${ANSI_CLEAR_LINE}≻ ${event.toolName}(${JSON.stringify(event.args)})\n`);
				break;

			case "tool_execution_end":
				this.output(`${ANSI_CLEAR_LINE}[${event.isError ? "Error" : "Result"}: ${this.toolResultText(event.result)}]\n`);
				break;

			case "turn_end":
				// flush any pending streaming output
				break;

			case "agent_end":
				this.output(`${ANSI_CLEAR_LINE}Done.\n`);
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

	private renderStreamingAssistant(message: AssistantMessage): void {
		let text = "";
		for (const content of message.content) {
			if (content.type === "text") {
				text += content.text;
			} else if (content.type === "thinking" && this.showThinking) {
				text += `[thinking: ${content.thinking}] `;
			}
			// toolCall content is handled via tool_execution_start events
		}

		if (text !== this.currentAssistantText) {
			this.currentAssistantText = text;
			this.output(`${ANSI_CLEAR_LINE}${ANSI_HIDE_CURSOR}${text}${ANSI_SHOW_CURSOR}`);
		}
	}

	private flushStreamingAssistant(message: AssistantMessage): void {
		let text = "";
		for (const content of message.content) {
			if (content.type === "text") {
				text += content.text;
			}
		}
		if (text !== this.currentAssistantText) {
			this.currentAssistantText = text;
		}
	}
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
