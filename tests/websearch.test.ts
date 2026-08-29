/**
 * Tests for the real websearch tool (DuckDuckGo Instant Answer + Wikipedia
 * fallback). Network calls are stubbed — these are deterministic.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { websearchTool, formatSearchResults } = await import("../src/tools/websearch.ts");

function jsonResponse(data: unknown, status = 200): Response {
	return new Response(JSON.stringify(data), {
		status,
		headers: { "content-type": "application/json" },
	});
}

const ddgPayload = {
	Heading: "OpenAI",
	AbstractText: "OpenAI is an American artificial intelligence company.",
	AbstractURL: "https://duckduckgo.com/OpenAI",
	Answer: "",
	RelatedTopics: [
		{ FirstURL: "https://en.wikipedia.org/wiki/OpenAI", Text: "OpenAI - WikipediaOpenAI is an AI lab." },
		{ FirstURL: "https://openai.com", Text: "OpenAI official site." },
	],
};

describe("websearchTool", () => {
	let originalFetch: typeof globalThis.fetch;
	beforeEach(() => {
		originalFetch = globalThis.fetch;
	});
	afterEach(() => {
		globalThis.fetch = originalFetch;
		vi.restoreAllMocks();
	});

	it("formats DuckDuckGo abstract + related results", async () => {
		globalThis.fetch = vi.fn(async () => jsonResponse(ddgPayload)) as any;
		const r = await websearchTool.execute("t1", { query: "openai" });
		expect(r.isError).toBeFalsy();
		const text = (r.content[0] as any).text;
		expect(text).toContain("OpenAI is an American artificial intelligence company.");
		expect(text).toContain("https://en.wikipedia.org/wiki/OpenAI");
		expect(text).toContain("https://openai.com");
	});

	it("falls back to Wikipedia when DuckDuckGo has nothing", async () => {
		const calls: string[] = [];
		globalThis.fetch = vi.fn(async (input: any) => {
			const url = String(input);
			calls.push(url);
			if (url.includes("api.duckduckgo.com")) {
				return jsonResponse({ Heading: "", AbstractText: "", AbstractURL: "", RelatedTopics: [] });
			}
			return jsonResponse([
				"query",
				["OpenAI", "OpenAI o1"],
				["desc1", "desc2"],
				["https://en.wikipedia.org/wiki/OpenAI", "https://en.wikipedia.org/wiki/OpenAI_o1"],
			]);
		}) as any;
		const r = await websearchTool.execute("t1", { query: "openai" });
		expect(r.isError).toBeFalsy();
		const text = (r.content[0] as any).text;
		expect(text).toContain("https://en.wikipedia.org/wiki/OpenAI_o1");
		expect(calls.some((u) => u.includes("en.wikipedia.org"))).toBe(true);
	});

	it("falls back to Wikipedia when DuckDuckGo is unreachable", async () => {
		globalThis.fetch = vi.fn(async (input: any) => {
			if (String(input).includes("api.duckduckgo.com")) throw new Error("network down");
			return jsonResponse(["q", ["Thing"], ["d"], ["https://en.wikipedia.org/wiki/Thing"]]);
		}) as any;
		const r = await websearchTool.execute("t1", { query: "thing" });
		expect(r.isError).toBeFalsy();
		expect((r.content[0] as any).text).toContain("https://en.wikipedia.org/wiki/Thing");
	});

	it("returns an error result when both backends fail", async () => {
		globalThis.fetch = vi.fn(async () => {
			throw new Error("network down");
		}) as any;
		const r = await websearchTool.execute("t1", { query: "anything" });
		expect(r.isError).toBe(true);
		expect((r.content[0] as any).text).toContain("Error");
	});

	it("rejects an empty query", async () => {
		const r = await websearchTool.execute("t1", { query: "   " });
		expect(r.isError).toBe(true);
	});

	it("formatSearchResults caps the number of listed hits", () => {
		const payload = {
			hits: Array.from({ length: 8 }, (_, i) => ({
				title: `t${i}`,
				url: `https://x/${i}`,
				snippet: `s${i}`,
			})),
			source: "wikipedia" as const,
		};
		const out = formatSearchResults(payload, 3);
		expect(out).toContain("1. t0");
		expect(out).toContain("3. t2");
		expect(out).not.toContain("4. t3");
	});

	it("formatSearchResults notes when nothing was found", () => {
		const out = formatSearchResults({ hits: [], source: "duckduckgo" }, 5);
		expect(out).toBe("(no results found)");
	});
});
