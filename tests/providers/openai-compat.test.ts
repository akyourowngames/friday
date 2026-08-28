import { describe, it, expect } from "vitest";
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

	it("listOpenAICompatModels handles errors gracefully", async () => {
		// Should not throw even with invalid key
		const models = await listOpenAICompatModels({
			apiKey: "sk-invalid",
			baseUrl: "https://nonexistent-host.invalid",
		});
		expect(Array.isArray(models)).toBe(true);
	});
});
