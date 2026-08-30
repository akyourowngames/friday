import { describe, it, expect, vi, beforeEach } from "vitest";

const googleMock = vi.hoisted(() => ({ request: undefined as any }));

vi.mock("@google/genai", () => ({
	GoogleGenAI: class {
		models = {
			generateContentStream: vi.fn(async (request: any) => {
				googleMock.request = request;
				return { async *[Symbol.asyncIterator]() {} };
			}),
			list: vi.fn(async () => { throw new Error("unavailable"); }),
		};
	},
}));

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

	it("transports tool-result images as function response inline data", async () => {
		const { createGoogleStreamFn } = await import("../../src/providers/google.ts");
		const streamFn = createGoogleStreamFn({ model: "gemini-test", apiKey: "AIza-test" });
		await streamFn({ id: "gemini-test", name: "Gemini", api: "google", provider: "google", baseUrl: "", reasoning: false, input: ["text", "image"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 }, contextWindow: 1000000, maxTokens: 8192 } as any, {
			messages: [{ role: "toolResult", toolCallId: "call_1", toolName: "capture", content: [{ type: "text", text: "done" }, { type: "image", data: "aGVsbG8=", mimeType: "image/webp" }], isError: false, timestamp: 0 }],
		});
		await vi.waitFor(() => expect(googleMock.request).toBeDefined());
		expect(googleMock.request.contents[0].parts[0].functionResponse).toEqual({
			id: "call_1",
			name: "capture",
			response: { result: "done" },
			parts: [{ inlineData: { data: "aGVsbG8=", mimeType: "image/webp" } }],
		});
	});

	it("listGoogleModels returns fallback list when SDK unavailable", async () => {
		const { listGoogleModels } = await import("../../src/providers/google.ts");
		const models = await listGoogleModels({ apiKey: "AIza-test" });
		expect(Array.isArray(models)).toBe(true);
		expect(models.length).toBeGreaterThan(0);
		expect(models).toContain("gemini-2.0-flash");
	});
});
