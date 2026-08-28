import { describe, it, expect } from "vitest";
import type {
	Api,
	AssistantMessage,
	AssistantMessageEvent,
	AgentEvent,
	Model,
	Tool,
	ToolResult,
	Usage,
} from "../src/types.ts";

describe("types", () => {
	it("should have compatible message types", () => {
		const userMsg = { role: "user" as const, content: "hello", timestamp: Date.now() };
		const assistantMsg: AssistantMessage = {
			role: "assistant",
			content: [{ type: "text", text: "hi" }],
			api: "faux",
			provider: "faux",
			model: "faux-1",
			usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
			stopReason: "stop",
			timestamp: Date.now(),
		};
		const toolResultMsg = {
			role: "toolResult" as const,
			toolCallId: "abc123",
			toolName: "calculator",
			content: [{ type: "text" as const, text: "4" }],
			isError: false,
			timestamp: Date.now(),
		};

		expect(userMsg.role).toBe("user");
		expect(assistantMsg.role).toBe("assistant");
		expect(toolResultMsg.role).toBe("toolResult");
	});

	it("should construct valid ToolResult", () => {
		const result: ToolResult = {
			content: [{ type: "text", text: "Hello from tool" }],
		};
		expect(result.content[0]?.type).toBe("text");
		expect((result.content[0] as any).text).toBe("Hello from tool");
	});

	it("AgentEvent union should include all lifecycle events", () => {
		const events: AgentEvent[] = [
			{ type: "agent_start" },
			{ type: "agent_end", messages: [] },
			{ type: "turn_start" },
			{ type: "turn_end", message: { role: "assistant", content: [], api: "faux", provider: "faux", model: "m", usage: {} as Usage, stopReason: "stop", timestamp: 0 }, toolResults: [] },
			{ type: "message_start", message: { role: "user", content: "hi", timestamp: 0 } },
			{ type: "message_update", message: { role: "assistant", content: [{ type: "text", text: "hi" }], api: "faux", provider: "faux", model: "m", usage: {} as Usage, stopReason: "stop", timestamp: 0 }, assistantMessageEvent: { type: "text_delta", contentIndex: 0, delta: "hi", partial: {} as any } },
			{ type: "message_end", message: { role: "assistant", content: [], api: "faux", provider: "faux", model: "m", usage: {} as Usage, stopReason: "stop", timestamp: 0 } },
		];

		expect(events.length).toBe(7);
		expect(events.every((e) => "type" in e)).toBe(true);
	});
});
