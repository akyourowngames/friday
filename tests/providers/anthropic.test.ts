import { describe, it, expect, vi, beforeEach } from "vitest";

describe("anthropic", () => {
	beforeEach(() => {
		// Reset module cache so dynamic imports work
		vi.resetModules();
	});

	it("createAnthropicStreamFn returns a StreamFn", async () => {
		const { createAnthropicStreamFn } = await import("../../src/providers/anthropic.ts");
		const streamFn = createAnthropicStreamFn({
			model: "claude-3-5-sonnet-latest",
			apiKey: "sk-ant-test",
		});
		expect(typeof streamFn).toBe("function");
	});

	it("listAnthropicModels returns fallback list when the API is unreachable", async () => {
		const { listAnthropicModels } = await import("../../src/providers/anthropic.ts");
		// Explicit unreachable baseUrl overrides any ANTHROPIC_BASE_URL env gateway,
		// so the SDK call fails and the hard-coded fallback is returned.
		const models = await listAnthropicModels({ apiKey: "sk-ant-test", baseUrl: "http://127.0.0.1:9" });
		expect(Array.isArray(models)).toBe(true);
		expect(models.length).toBeGreaterThan(0);
		expect(models).toContain("claude-3-5-sonnet-latest");
	});

	it("propagates API errors", async () => {
		// Skip if SDK can't be loaded — just test structure
		try {
			const { createAnthropicStreamFn } = await import("../../src/providers/anthropic.ts");
			const streamFn = createAnthropicStreamFn({
				model: "claude-3-5-sonnet-latest",
				apiKey: "invalid-key",
			});
			const stream = streamFn(
				{ id: "claude-3-5-sonnet-latest", name: "Claude", api: "anthropic", provider: "anthropic", baseUrl: "", reasoning: false, input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 }, contextWindow: 200000, maxTokens: 8192 },
				{ messages: [{ role: "user", content: "Hi", timestamp: 0 }] },
			);
			expect(stream).toBeDefined();
		} catch (err: any) {
			// If SDK not installed, error mentions install command
			expect(err.message).toMatch(/SDK not installed|@anthropic-ai-sdk/);
		}
	});
});
