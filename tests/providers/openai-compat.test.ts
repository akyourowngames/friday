import { describe, it, expect, vi } from "vitest";

const openAIMock = vi.hoisted(() => ({ params: undefined as any }));

vi.mock("openai", () => ({
	default: class {
		chat = {
			completions: {
				create: vi.fn(async (params: any) => {
					openAIMock.params = params;
					return { async *[Symbol.asyncIterator]() {} };
				}),
			},
		};
		models = { list: vi.fn(async () => { throw new Error("unavailable"); }) };
	},
}));

import { createOpenAICompatStreamFn, listOpenAICompatModels } from "../../src/providers/openai-compat.ts";

describe("openai-compat", () => {
	it("createOpenAICompatStreamFn returns a StreamFn", () => {
		const streamFn = createOpenAICompatStreamFn({
			model: "test-model",
			apiKey: "sk-test",
			baseUrl: "https://api.example.com/v1",
		});
		expect(typeof streamFn).toBe("function");
	});

	it("createOpenAICompatStreamFn accepts a custom baseUrl", () => {
		const streamFn = createOpenAICompatStreamFn({
			model: "llama3.2",
			apiKey: "ollama",
			baseUrl: "http://localhost:11434/v1",
		});
		expect(typeof streamFn).toBe("function");
	});

	it("returned streamFn produces an AssistantMessageEventStream", () => {
		const streamFn = createOpenAICompatStreamFn({
			model: "gpt-4o",
			apiKey: "sk-test",
		});

		const model = {
			id: "gpt-4o",
			name: "GPT-4o",
			api: "openai",
			provider: "openai",
			baseUrl: "",
			reasoning: false,
			input: ["text"],
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			contextWindow: 4096,
			maxTokens: 2048,
		};

		const stream = streamFn(model, {
			messages: [{ role: "user", content: "Hi", timestamp: 0 }],
		});

		expect(stream).toBeDefined();
		expect(typeof stream[Symbol.asyncIterator]).toBe("function");
	});

	it("transports tool-result images as linked user data URIs", async () => {
		const streamFn = createOpenAICompatStreamFn({ model: "gpt-4o", apiKey: "sk-test" });
		const model = { id: "gpt-4o", name: "GPT-4o", api: "openai", provider: "openai", baseUrl: "", reasoning: false, input: ["text", "image"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 }, contextWindow: 4096, maxTokens: 2048 } as any;
		streamFn(model, {
			messages: [{ role: "toolResult", toolCallId: "call_1", toolName: "capture", content: [{ type: "text", text: "done" }, { type: "image", data: "aGVsbG8=", mimeType: "image/png" }], isError: false, timestamp: 0 }],
		});
		await vi.waitFor(() => expect(openAIMock.params).toBeDefined());
		expect(openAIMock.params.messages).toEqual([
			{ role: "tool", tool_call_id: "call_1", content: "done" },
			{ role: "user", content: [
				{ type: "text", text: "Images returned by tool result call_1 (capture):" },
				{ type: "image_url", image_url: { url: "data:image/png;base64,aGVsbG8=" } },
			] },
		]);
	});

	it("listOpenAICompatModels handles errors gracefully", async () => {
		// Should not throw even with invalid key
		const models = await listOpenAICompatModels({
			apiKey: "sk-invalid",
			baseUrl: "https://nonexistent-host.invalid",
		});
		expect(Array.isArray(models)).toBe(true);
	});
});
