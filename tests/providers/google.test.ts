import { describe, it, expect, vi, beforeEach } from "vitest";

describe("google", () => {
	beforeEach(() => {
		vi.resetModules();
	});

	it("createGoogleStreamFn returns a StreamFn", async () => {
		const { createGoogleStreamFn } = await import("../../src/providers/google.ts");
		const streamFn = createGoogleStreamFn({
			model: "gemini-2.0-flash",
			apiKey: "AIza-test",
		});
		expect(typeof streamFn).toBe("function");
	});

	it("listGoogleModels returns fallback list when SDK unavailable", async () => {
		const { listGoogleModels } = await import("../../src/providers/google.ts");
		const models = await listGoogleModels({ apiKey: "AIza-test" });
		expect(Array.isArray(models)).toBe(true);
		expect(models.length).toBeGreaterThan(0);
		expect(models).toContain("gemini-2.0-flash");
	});
});
