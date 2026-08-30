import { describe, it, expect, vi } from "vitest";

describe("freecc", () => {
	it("createFreeccStreamFn returns a StreamFn", async () => {
		const { createFreeccStreamFn } = await import("../../src/providers/freecc.ts");
		const streamFn = createFreeccStreamFn({
			model: "claude-3-freecc-no-thinking/kilo/tencent/hy3:free",
			apiKey: "freecc",
		});
		expect(typeof streamFn).toBe("function");
	});

	it("transports tool-result images as Anthropic base64 blocks", async () => {
		const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("", { status: 200 }));
		const { createFreeccStreamFn } = await import("../../src/providers/freecc.ts");
		const streamFn = createFreeccStreamFn({ model: "freecc-test", apiKey: "freecc", baseUrl: "http://freecc.test" });
		await streamFn({ id: "freecc-test", name: "Freecc", api: "freecc", provider: "freecc", baseUrl: "", reasoning: false, input: ["text", "image"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 }, contextWindow: 200000, maxTokens: 8192 } as any, {
			messages: [{ role: "toolResult", toolCallId: "tool_1", toolName: "capture", content: [{ type: "text", text: "done" }, { type: "image", data: "aGVsbG8=", mimeType: "image/gif" }], isError: false, timestamp: 0 }],
		});
		await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
		const body = JSON.parse(fetchMock.mock.calls[0][1]?.body as string);
		expect(body.messages[0].content[0].content).toEqual([
			{ type: "text", text: "done" },
			{ type: "image", source: { type: "base64", media_type: "image/gif", data: "aGVsbG8=" } },
		]);
		fetchMock.mockRestore();
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
