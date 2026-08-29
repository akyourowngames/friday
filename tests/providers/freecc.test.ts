import { describe, it, expect } from "vitest";

describe("freecc", () => {
	it("createFreeccStreamFn returns a StreamFn", async () => {
		const { createFreeccStreamFn } = await import("../../src/providers/freecc.ts");
		const streamFn = createFreeccStreamFn({
			model: "claude-3-freecc-no-thinking/kilo/tencent/hy3:free",
			apiKey: "freecc",
		});
		expect(typeof streamFn).toBe("function");
	});

	it("listFreeccModels returns an empty array on connection error", async () => {
		const { listFreeccModels } = await import("../../src/providers/freecc.ts");
		// Explicit unreachable baseUrl so the fetch fails fast and we hit the
		// catch block. This guards against regressions in the error path.
		const models = await listFreeccModels({ apiKey: "freecc", baseUrl: "http://127.0.0.1:9" });
		expect(Array.isArray(models)).toBe(true);
		expect(models).toEqual([]);
	});
});
