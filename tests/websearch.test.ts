/**
 * Tests for the websearch tool. Network calls are stubbed via `vi.fn`
 * so the suite stays deterministic and offline.
 *
 * Covers: each individual provider, the chain/race logic, the format
 * function, key/url wiring, and the user-facing tool wrapper.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const {
	websearchTool,
	websearchToolWith,
	formatSearchResults,
	runWebsearch,
} = await import("../src/tools/websearch.ts");

function jsonResponse(data: unknown, status = 200, headers: Record<string, string> = {}): Response {
	return new Response(JSON.stringify(data), {
		status,
		headers: { "content-type": "application/json", ...headers },
	});
}

function htmlResponse(body: string, status = 200): Response {
	return new Response(body, {
		status,
		headers: { "content-type": "text/html" },
	});
}

const DDG_HTML_FIXTURE = `
<div class="result">
	<div class="links_main">
		<a class="result__a" href="https://example.com/openai">OpenAI - example<span>OpenAI is an AI lab</span></a>
	</div>
	<a class="result__snippet" href="https://example.com/openai">OpenAI is an American AI research lab.</a>
</div>
<div class="result">
	<div class="links_main">
		<a class="result__a" href="https://example.com/gpt4">GPT-4 explained</a>
	</div>
	<a class="result__snippet">A breakdown of capabilities.</a>
</div>
`;

const TAVILY_PAYLOAD = {
	answer: "OpenAI was founded in 2015.",
	results: [
		{ title: "OpenAI", url: "https://openai.com", content: "OpenAI is an AI lab." },
		{ title: "GPT-4", url: "https://openai.com/gpt-4", content: "About the GPT-4 model." },
	],
};

const SEARXNG_PAYLOAD = {
	results: [
		{ title: "OpenAI - Wikipedia", url: "https://en.wikipedia.org/wiki/OpenAI", content: "OpenAI is an AI lab." },
		{ title: "OpenAI - Crunchbase", url: "https://crunchbase.com/organization/openai", content: "Company info." },
	],
};

const DDG_IA_PAYLOAD = {
	Heading: "OpenAI",
	AbstractText: "OpenAI is an American AI company.",
	AbstractURL: "https://duckduckgo.com/OpenAI",
	Answer: "",
	RelatedTopics: [
		{ FirstURL: "https://en.wikipedia.org/wiki/OpenAI", Text: "OpenAI - Wikipedia" },
		{ FirstURL: "https://openai.com", Text: "OpenAI official" },
	],
};

const WIKIPEDIA_PAYLOAD = [
	"openai",
	["OpenAI", "OpenAI o1"],
	["desc1", "desc2"],
	["https://en.wikipedia.org/wiki/OpenAI", "https://en.wikipedia.org/wiki/OpenAI_o1"],
];

describe("websearch providers", () => {
	let originalFetch: typeof globalThis.fetch;
	beforeEach(() => {
		originalFetch = globalThis.fetch;
	});
	afterEach(() => {
		globalThis.fetch = originalFetch;
		vi.restoreAllMocks();
	});

	it("DuckDuckGo HTML: parses result__a + result__snippet blocks", async () => {
		globalThis.fetch = vi.fn(async (input: any) => {
			if (String(input).includes("html.duckduckgo.com")) return htmlResponse(DDG_HTML_FIXTURE);
			return new Response("not used", { status: 404 });
		}) as any;
		const { merged, results } = await runWebsearch("openai", {});
		const ddg = results.find((r) => r.provider === "duckduckgo-html");
		expect(ddg?.status).toBe("ok");
		expect(merged.hits.length).toBeGreaterThanOrEqual(2);
		expect(merged.hits[0]?.title).toContain("OpenAI");
		expect(merged.hits[0]?.url).toBe("https://example.com/openai");
	});

	it("Tavily: returns the answer + results when the key is configured", async () => {
		globalThis.fetch = vi.fn(async (input: any, init: any) => {
			const url = String(input);
			if (url.includes("api.tavily.com")) {
				const body = JSON.parse((init?.body as string) ?? "{}");
				expect(body.api_key).toBe("tvly-test");
				expect(body.query).toBe("openai");
				return jsonResponse(TAVILY_PAYLOAD);
			}
			return new Response("not used", { status: 404 });
		}) as any;
		const { merged, results } = await runWebsearch("openai", { config: { tavilyApiKey: "tvly-test" } });
		const tavily = results.find((r) => r.provider === "tavily");
		expect(tavily?.status).toBe("ok");
		expect(merged.answer).toBe("OpenAI was founded in 2015.");
		expect(merged.hits[0]?.title).toBe("OpenAI");
	});

	it("SearXNG: hits a configured instance and parses JSON", async () => {
		globalThis.fetch = vi.fn(async (input: any) => {
			if (String(input).includes("search.example.com")) return jsonResponse(SEARXNG_PAYLOAD);
			return new Response("not used", { status: 404 });
		}) as any;
		const { merged, results } = await runWebsearch("openai", { config: { searxngUrl: "https://search.example.com" } });
		const sx = results.find((r) => r.provider === "searxng");
		expect(sx?.status).toBe("ok");
		expect(merged.hits[0]?.title).toBe("OpenAI - Wikipedia");
	});

	it("chain falls back to DDG IA + Wikipedia when HTML scrape returns nothing", async () => {
		globalThis.fetch = vi.fn(async (input: any) => {
			const url = String(input);
			if (url.includes("html.duckduckgo.com")) return htmlResponse("<html><body>no results</body></html>");
			if (url.includes("api.duckduckgo.com")) return jsonResponse(DDG_IA_PAYLOAD);
			return new Response("not used", { status: 404 });
		}) as any;
		const { merged, results } = await runWebsearch("openai", {});
		const ia = results.find((r) => r.provider === "duckduckgo");
		expect(ia?.status).toBe("ok");
		// DDG IA returns the abstract as a hit, plus its own RelatedTopics.
		expect(merged.hits.length).toBeGreaterThan(0);
	});

	it("falls all the way back to Wikipedia when every provider returns nothing", async () => {
		globalThis.fetch = vi.fn(async (input: any) => {
			const url = String(input);
			if (url.includes("html.duckduckgo.com")) return htmlResponse("<html></html>");
			if (url.includes("api.duckduckgo.com")) return jsonResponse({ Heading: "", AbstractText: "", AbstractURL: "", RelatedTopics: [] });
			if (url.includes("en.wikipedia.org")) return jsonResponse(WIKIPEDIA_PAYLOAD);
			return new Response("not used", { status: 404 });
		}) as any;
		const { merged, results } = await runWebsearch("openai", {});
		const wiki = results.find((r) => r.provider === "wikipedia");
		expect(wiki?.status).toBe("ok");
		expect(merged.heading).toBe("Wikipedia");
		expect(merged.hits[0]?.title).toBe("OpenAI");
	});

	it("deduplicates hits across providers by URL", async () => {
		globalThis.fetch = vi.fn(async (input: any) => {
			const url = String(input);
			if (url.includes("api.tavily.com")) return jsonResponse(TAVILY_PAYLOAD);
			if (url.includes("search.example.com")) return jsonResponse(SEARXNG_PAYLOAD);
			if (url.includes("html.duckduckgo.com")) return htmlResponse(DDG_HTML_FIXTURE);
			return new Response("not used", { status: 404 });
		}) as any;
		const { merged } = await runWebsearch("openai", { config: { tavilyApiKey: "k", searxngUrl: "https://search.example.com" } });
		const urls = merged.hits.map((h) => h.url);
		expect(new Set(urls).size).toBe(urls.length); // all unique
	});

	it("propagates abort to in-flight requests", async () => {
		let aborted = false;
		globalThis.fetch = vi.fn(async (_input: any, init: any) => {
			init?.signal?.addEventListener("abort", () => (aborted = true));
			return new Promise<Response>((_, reject) => {
				init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
			});
		}) as any;
		const ac = new AbortController();
		const promise = runWebsearch("openai", { signal: ac.signal });
		// Give the providers a tick to subscribe.
		await Promise.resolve();
		ac.abort();
		await promise.catch(() => undefined);
		expect(aborted).toBe(true);
	});
});

describe("websearch tool wrapper", () => {
	let originalFetch: typeof globalThis.fetch;
	beforeEach(() => {
		originalFetch = globalThis.fetch;
	});
	afterEach(() => {
		globalThis.fetch = originalFetch;
		vi.restoreAllMocks();
	});

	it("websearchTool (no config) still works with HTML scrape + fallbacks", async () => {
		globalThis.fetch = vi.fn(async (input: any) => {
			const url = String(input);
			if (url.includes("html.duckduckgo.com")) return htmlResponse(DDG_HTML_FIXTURE);
			return new Response("not used", { status: 404 });
		}) as any;
		const r = await websearchTool.execute("t1", { query: "openai" });
		expect(r.isError).toBeFalsy();
		const text = (r.content[0] as any).text;
		expect(text).toContain("OpenAI");
		// Details should include the structured results so the UI can render a card.
		const details = r.details as { results?: unknown[]; provider?: string; sources?: unknown[] };
		expect(Array.isArray(details.results)).toBe(true);
		expect(Array.isArray(details.sources)).toBe(true);
	});

	it("websearchToolWith({ tavilyApiKey }) surfaces the answer in the model-facing text", async () => {
		globalThis.fetch = vi.fn(async (input: any, init: any) => {
			const url = String(input);
			if (url.includes("api.tavily.com")) {
				const body = JSON.parse((init?.body as string) ?? "{}");
				expect(body.api_key).toBe("tvly-bound");
				return jsonResponse(TAVILY_PAYLOAD);
			}
			return new Response("not used", { status: 404 });
		}) as any;
		const bound = websearchToolWith({ tavilyApiKey: "tvly-bound" });
		const r = await bound.execute("t1", { query: "openai" });
		expect(r.isError).toBeFalsy();
		const text = (r.content[0] as any).text;
		expect(text).toContain("Summary: OpenAI was founded in 2015.");
		expect(text).toContain("[1] OpenAI");
	});

	it("rejects an empty query", async () => {
		const r = await websearchTool.execute("t1", { query: "   " });
		expect(r.isError).toBe(true);
	});

	it("reports an error when every provider fails", async () => {
		globalThis.fetch = vi.fn(async () => {
			throw new Error("network down");
		}) as any;
		const r = await websearchTool.execute("t1", { query: "anything" });
		expect(r.isError).toBe(true);
	});
});

describe("formatSearchResults", () => {
	it("emits a Summary line when one is available", () => {
		const out = formatSearchResults(
			{
				answer: "TL;DR",
				hits: [{ title: "t", url: "https://x", snippet: "s" }],
				sources: [],
			},
			5,
		);
		expect(out).toContain("Summary: TL;DR");
		expect(out).toContain("[1] t");
	});

	it("emits a Sources consulted footer with per-provider counts", () => {
		const out = formatSearchResults(
			{
				hits: [{ title: "t", url: "https://x", snippet: "s" }],
				sources: [
					{ provider: "duckduckgo-html", status: "ok", latencyMs: 220, hitCount: 3 },
					{ provider: "wikipedia", status: "ok", latencyMs: 410, hitCount: 0 },
					{ provider: "tavily", status: "error", latencyMs: 999, error: "401" },
				],
			},
			5,
		);
		expect(out).toContain("Sources consulted: duckduckgo-html(3 hits, 220ms).");
		expect(out).not.toContain("wikipedia");
		expect(out).not.toContain("tavily");
	});

	it("falls back to a clear 'no results' line when nothing came back", () => {
		const out = formatSearchResults({ hits: [], sources: [] }, 5);
		expect(out).toBe("(no results found across any provider)");
	});

	it("caps the number of listed hits", () => {
		const out = formatSearchResults(
			{
				hits: Array.from({ length: 8 }, (_, i) => ({ title: `t${i}`, url: `https://x/${i}`, snippet: `s${i}` })),
				sources: [],
			},
			3,
		);
		expect(out).toContain("[1] t0");
		expect(out).toContain("[3] t2");
		expect(out).not.toContain("[4] t3");
	});
});
