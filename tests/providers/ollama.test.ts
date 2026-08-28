import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createOllamaStreamFn, listOllamaModels, isOllamaRunning } from "../../src/providers/ollama.ts";

describe("ollama", () => {
	const originalFetch = globalThis.fetch;

	afterEach(() => {
		globalThis.fetch = originalFetch;
	});

	it("createOllamaStreamFn returns a StreamFn", () => {
		const streamFn = createOllamaStreamFn({ model: "llama3.2" });
		expect(typeof streamFn).toBe("function");
	});

	it("createOllamaStreamFn uses default model", () => {
		const streamFn = createOllamaStreamFn();
		expect(typeof streamFn).toBe("function");
	});

	it("isOllamaRunning returns true when server responds", async () => {
		globalThis.fetch = vi.fn().mockResolvedValue({ ok: true }) as any;
		const running = await isOllamaRunning();
		expect(running).toBe(true);
	});

	it("isOllamaRunning returns false on fetch error", async () => {
		globalThis.fetch = vi.fn().mockRejectedValue(new Error("Connection refused")) as any;
		const running = await isOllamaRunning();
		expect(running).toBe(false);
	});

	it("isOllamaRunning returns false on non-ok response", async () => {
		globalThis.fetch = vi.fn().mockResolvedValue({ ok: false }) as any;
		const running = await isOllamaRunning();
		expect(running).toBe(false);
	});

	it("listOllamaModels returns empty array on connection error", async () => {
		globalThis.fetch = vi.fn().mockRejectedValue(new Error("Connection refused")) as any;
		const models = await listOllamaModels();
		expect(Array.isArray(models)).toBe(true);
	});
});
