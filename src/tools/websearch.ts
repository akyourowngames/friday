/**
 * Real web search for friday-ng.
 *
 * Chains several providers in parallel and returns the first useful result,
 * with fallbacks so the tool keeps working even when a backend is slow or
 * rate-limited. Configurable via the standard `config.providers.<id>.key`
 * (or env) for the paid backends, and via a new `settings.search*` block
 * for the SearXNG instance URL.
 *
 * Providers (in race order):
 *  1. DuckDuckGo HTML  — no key, scrapes the lite HTML endpoint
 *  2. Tavily            — key required, AI-native answers + citations
 *  3. SearXNG           — no key, configurable instance URL
 *  4. DDG Instant Answer — no key, original JSON path
 *  5. Wikipedia opensearch — final fallback
 *
 * The model receives:
 *  - a short synthesized answer,
 *  - a numbered list of references (title, snippet, url),
 *  - the list of providers that contributed, for citation transparency.
 */
import { Type } from "typebox";
import type { AgentTool, ToolResult } from "../types.ts";

const WEBSEARCH_TIMEOUT_MS = 12_000;
const MAX_RESULTS = 8;
const USER_AGENT = "friday-ng/0.3 (websearch tool)";

export const websearchParams = Type.Object({
	query: Type.String({ description: "Search query" }),
	numResults: Type.Optional(
		Type.Integer({ description: "Maximum related results to return (default 6)", minimum: 1, maximum: MAX_RESULTS }),
	),
	recency: Type.Optional(
		Type.Union([Type.Literal("any"), Type.Literal("day"), Type.Literal("week"), Type.Literal("month"), Type.Literal("year")], {
			description: "How recent the results should be (Tavily + DDG only).",
		}),
	),
});

export interface SearchHit {
	title: string;
	url: string;
	snippet: string;
}

export interface SearchPayload {
	heading?: string;
	answer?: string;
	hits: SearchHit[];
	sources?: ProviderSource[];
	totalHits?: number;
}

export interface ProviderSource {
	provider: SearchProviderId;
	status: "ok" | "no-results" | "error" | "skipped";
	latencyMs?: number;
	error?: string;
	hitCount?: number;
}

export type SearchProviderId = "duckduckgo-html" | "tavily" | "searxng" | "duckduckgo" | "wikipedia";

/** Per-tool config: keys + URLs. Sourced from `config` in the route layer. */
export interface WebsearchConfig {
	tavilyApiKey?: string;
	searxngUrl?: string;
	timeoutMs?: number;
}

function errorResult(message: string): ToolResult {
	return { content: [{ type: "text" as const, text: `Error: ${message}` }], details: { error: true }, isError: true };
}

function isPayloadUseful(p: SearchPayload | undefined): p is SearchPayload {
	if (!p) return false;
	return Boolean(p.answer) || p.hits.length > 0;
}

function trimSnippet(s: string, max = 280): string {
	const t = s.replace(/\s+/g, " ").trim();
	return t.length > max ? `${t.slice(0, max - 1)}…` : t;
}

// -------------------------------------------------------------------------
// Provider 1: DuckDuckGo HTML (no key) — scrapes the lite HTML endpoint.
// Reliable, real top-10 web results, no rate limit for reasonable traffic.
// -------------------------------------------------------------------------
async function searchDuckDuckGoHtml(query: string, signal?: AbortSignal): Promise<SearchPayload> {
	const url = `https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`;
	const res = await fetch(url, {
		headers: { "User-Agent": USER_AGENT, Accept: "text/html" },
		redirect: "follow",
		signal: signal ?? AbortSignal.timeout(WEBSEARCH_TIMEOUT_MS),
	});
	if (!res.ok) throw new Error(`DuckDuckGo HTML returned HTTP ${res.status}`);
	const html = await res.text();
	const hits = parseDuckDuckGoHtml(html);
	return { hits, sources: [] };
}

/** Extract result blocks from DDG's lite HTML. The structure is stable but
 *  undocumented, so we lean on the class names DDG itself uses. */
function parseDuckDuckGoHtml(html: string): SearchHit[] {
	const out: SearchHit[] = [];
	// Walk every <a class="result__a" ...> ... </a> block. Each result
	// card has one anchor with the title, and an optional adjacent
	// "result__snippet" anchor with the snippet text. We pair them by
	// walking the document in order.
	const linkRe = /<a[^>]*class="[^"]*\bresult__a\b[^"]*"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/g;
	const snippetRe = /<a[^>]*class="[^"]*\bresult__snippet\b[^"]*"[^>]*>(?:[\s\S]*?href="([^"]*)"[^>]*)?([\s\S]*?)<\/a>/g;
	const titles: Array<{ url: string; title: string }> = [];
	for (const m of html.matchAll(linkRe)) {
		titles.push({ url: decodeHtml(m[1]!), title: stripTags(decodeHtml(m[2]!)) });
	}
	const snippets: string[] = [];
	for (const m of html.matchAll(snippetRe)) {
		// The snippet anchor's body is the text; the href is the same
		// URL as the title anchor in the same card.
		const text = stripTags(decodeHtml(m[2] ?? ""));
		snippets.push(text);
	}
	for (let i = 0; i < titles.length; i++) {
		const t = titles[i]!;
		if (!t.url || !t.title) continue;
		out.push({ title: t.title, url: t.url, snippet: trimSnippet(snippets[i] ?? "") });
		if (out.length >= MAX_RESULTS) break;
	}
	return out;
}

function stripTags(s: string): string {
	return s.replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim();
}

function decodeHtml(s: string): string {
	return s
		.replace(/&amp;/g, "&")
		.replace(/&lt;/g, "<")
		.replace(/&gt;/g, ">")
		.replace(/&quot;/g, '"')
		.replace(/&#39;/g, "'")
		.replace(/&nbsp;/g, " ");
}

// -------------------------------------------------------------------------
// Provider 2: Tavily — key required, AI-native search optimized for agents.
// Free tier at https://tavily.com (1000 queries/month).
// -------------------------------------------------------------------------
async function searchTavily(query: string, apiKey: string, signal: AbortSignal | undefined, numResults: number, recency?: string): Promise<SearchPayload> {
	const res = await fetch("https://api.tavily.com/search", {
		method: "POST",
		headers: { "content-type": "application/json" },
		body: JSON.stringify({
			api_key: apiKey,
			query,
			max_results: Math.min(numResults, MAX_RESULTS),
			search_depth: "advanced",
			include_answer: "advanced",
			include_raw_content: false,
			days: recencyToDays(recency),
		}),
		signal: signal ?? AbortSignal.timeout(WEBSEARCH_TIMEOUT_MS),
	});
	if (!res.ok) throw new Error(`Tavily returned HTTP ${res.status}`);
	const data: any = await res.json();
	const raw: any[] = Array.isArray(data?.results) ? data.results : [];
	const hits: SearchHit[] = raw.slice(0, numResults).map((r) => ({
		title: String(r?.title ?? ""),
		url: String(r?.url ?? ""),
		snippet: trimSnippet(String(r?.content ?? "")),
	}));
	return {
		answer: typeof data?.answer === "string" && data.answer ? trimSnippet(data.answer, 800) : undefined,
		hits,
	};
}

function recencyToDays(recency?: string): number | undefined {
	switch (recency) {
		case "day": return 1;
		case "week": return 7;
		case "month": return 30;
		case "year": return 365;
		default: return undefined;
	}
}

// -------------------------------------------------------------------------
// Provider 3: SearXNG — no key, configurable instance URL.
// Aggregates results from many engines; privacy-friendly.
// -------------------------------------------------------------------------
async function searchSearxng(query: string, instanceUrl: string, signal: AbortSignal | undefined, numResults: number): Promise<SearchPayload> {
	const base = instanceUrl.replace(/\/+$/, "");
	const url = `${base}/search?q=${encodeURIComponent(query)}&format=json&categories=general&language=en-US&safesearch=0&count=${Math.min(numResults, MAX_RESULTS)}`;
	const res = await fetch(url, {
		headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
		signal: signal ?? AbortSignal.timeout(WEBSEARCH_TIMEOUT_MS),
	});
	if (!res.ok) throw new Error(`SearXNG returned HTTP ${res.status}`);
	const data: any = await res.json();
	const raw: any[] = Array.isArray(data?.results) ? data.results : [];
	const hits: SearchHit[] = raw.slice(0, numResults).map((r) => ({
		title: String(r?.title ?? ""),
		url: String(r?.url ?? ""),
		snippet: trimSnippet(String(r?.content ?? "")),
	}));
	return { hits, sources: [] };
}

// -------------------------------------------------------------------------
// Provider 4: DDG Instant Answer (original JSON path, kept for backwards compat).
// -------------------------------------------------------------------------
async function searchDuckDuckGoIA(query: string, signal?: AbortSignal): Promise<SearchPayload | undefined> {
	const url = `https://api.duckduckgo.com/?q=${encodeURIComponent(query)}&format=json&no_html=1&skip_disambig=1`;
	const res = await fetch(url, {
		headers: { "User-Agent": USER_AGENT },
		signal: signal ?? AbortSignal.timeout(WEBSEARCH_TIMEOUT_MS),
	});
	if (!res.ok) throw new Error(`DuckDuckGo returned HTTP ${res.status}`);
	const data: any = await res.json();
	const hits: SearchHit[] = [];
	const walkTopics = (topics: any[]): void => {
		for (const t of topics ?? []) {
			if (Array.isArray(t?.Topics)) walkTopics(t.Topics);
			else if (t?.FirstURL && t?.Text) {
				const text: string = t.Text;
				const title = text.split(/ - | – |: /, 1)[0] || text.slice(0, 80);
				hits.push({ title, url: t.FirstURL, snippet: trimSnippet(text) });
			}
		}
	};
	walkTopics(data?.RelatedTopics ?? []);
	const abstract: string = data?.AbstractText ?? data?.Abstract ?? "";
	const heading: string = data?.Heading ?? "";
	const hasAbstract = abstract.length > 0 && Boolean(data?.AbstractURL);
	if (!hasAbstract && hits.length === 0 && !data?.Answer) return undefined;
	return {
		heading: heading || undefined,
		answer: typeof data?.Answer === "string" && data.Answer.length > 0 ? data.Answer : undefined,
		hits,
	};
}

// -------------------------------------------------------------------------
// Provider 5: Wikipedia opensearch (final fallback).
// -------------------------------------------------------------------------
async function searchWikipedia(query: string, signal?: AbortSignal): Promise<SearchPayload> {
	const url = `https://en.wikipedia.org/w/api.php?action=opensearch&search=${encodeURIComponent(query)}&limit=${MAX_RESULTS}&namespace=0&format=json`;
	const res = await fetch(url, {
		headers: { "User-Agent": USER_AGENT },
		signal: signal ?? AbortSignal.timeout(WEBSEARCH_TIMEOUT_MS),
	});
	if (!res.ok) throw new Error(`Wikipedia returned HTTP ${res.status}`);
	const data: any = await res.json();
	const titles: string[] = data?.[1] ?? [];
	const urls: string[] = data?.[3] ?? [];
	const descriptions: string[] = data?.[2] ?? [];
	const hits: SearchHit[] = [];
	for (let i = 0; i < titles.length; i++) {
		if (!urls[i]) continue;
		hits.push({ title: titles[i], url: urls[i], snippet: trimSnippet(descriptions[i] ?? "") });
	}
	return { heading: "Wikipedia", hits, sources: [] };
}

// -------------------------------------------------------------------------
// Provider chain — run a configurable list in parallel, take the first
// non-empty payload, and backfill with hits from slower providers.
// -------------------------------------------------------------------------
export interface ProviderResult {
	provider: SearchProviderId;
	payload?: SearchPayload;
	status: "ok" | "no-results" | "error" | "skipped";
	latencyMs: number;
	error?: string;
	hitCount?: number;
}

async function runProvider(
	provider: SearchProviderId,
	runner: () => Promise<SearchPayload | undefined>,
): Promise<ProviderResult> {
	const t0 = performance.now();
	try {
		const payload = await runner();
		const ms = Math.round(performance.now() - t0);
		if (isPayloadUseful(payload)) {
			return { provider, payload, status: "ok", latencyMs: ms, hitCount: payload?.hits.length };
		}
		return { provider, status: "no-results", latencyMs: ms };
	} catch (e) {
		const ms = Math.round(performance.now() - t0);
		return { provider, status: "error", latencyMs: ms, error: e instanceof Error ? e.message : String(e) };
	}
}

function dedupeByUrl(hits: SearchHit[]): SearchHit[] {
	const seen = new Set<string>();
	const out: SearchHit[] = [];
	for (const h of hits) {
		const key = h.url.split("#")[0]!;
		if (seen.has(key)) continue;
		seen.add(key);
		out.push(h);
	}
	return out;
}

/** Run all configured providers in parallel and merge the best result. */
export async function runWebsearch(
	query: string,
	options: { numResults?: number; recency?: string; config?: WebsearchConfig; signal?: AbortSignal } = {},
): Promise<{ merged: SearchPayload; results: ProviderResult[] }> {
	const numResults = Math.min(options.numResults ?? 6, MAX_RESULTS);
	const recency = options.recency;
	const cfg = options.config ?? {};
	const overallSignal = options.signal;

	const plan: Array<{ id: SearchProviderId; run: (signal: AbortSignal) => Promise<SearchPayload | undefined> }> = [
		{ id: "duckduckgo-html", run: (sig) => searchDuckDuckGoHtml(query, sig) },
	];
	if (cfg.tavilyApiKey) {
		plan.push({ id: "tavily", run: (sig) => searchTavily(query, cfg.tavilyApiKey!, sig, numResults, recency) });
	}
	if (cfg.searxngUrl) {
		plan.push({ id: "searxng", run: (sig) => searchSearxng(query, cfg.searxngUrl!, sig, numResults) });
	}
	// Always-available fallbacks.
	plan.push({ id: "duckduckgo", run: (sig) => searchDuckDuckGoIA(query, sig) });
	plan.push({ id: "wikipedia", run: (sig) => searchWikipedia(query, sig) });

	const ac = new AbortController();
	const onAbort = () => ac.abort();
	overallSignal?.addEventListener("abort", onAbort, { once: true });
	const results = await Promise.all(
		plan.map((p) => runProvider(p.id, () => p.run(ac.signal))),
	);
	overallSignal?.removeEventListener("abort", onAbort);

	// Sort: ok with a payload first, then no-results, then errors.
	results.sort((a, b) => {
		const score = (r: ProviderResult) => (r.status === "ok" ? 0 : r.status === "no-results" ? 1 : 2);
		const s = score(a) - score(b);
		return s !== 0 ? s : a.latencyMs - b.latencyMs;
	});

	const winner = results.find((r) => r.status === "ok" && r.payload);
	const merged: SearchPayload = {
		heading: winner?.payload?.heading,
		answer: winner?.payload?.answer,
		hits: dedupeByUrl(
			results
				.filter((r) => r.status === "ok" && r.payload)
				.flatMap((r) => r.payload!.hits)
				.slice(0, numResults),
		),
		sources: results.map((r) => ({
			provider: r.provider,
			status: r.status,
			latencyMs: r.latencyMs,
			hitCount: r.payload?.hits.length,
			...(r.error ? { error: r.error } : {}),
		})),
		totalHits: results.reduce((n, r) => n + (r.payload?.hits.length ?? 0), 0),
	};

	return { merged, results };
}

// -------------------------------------------------------------------------
// Formatting
// -------------------------------------------------------------------------
/** Format a merged payload as readable text for the model. */
export function formatSearchResults(payload: SearchPayload, numResults: number): string {
	const parts: string[] = [];
	if (payload.answer) parts.push(`Summary: ${payload.answer}`);
	const hits = payload.hits.slice(0, numResults);
	if (hits.length > 0) {
		parts.push("References:");
		hits.forEach((h, i) => {
			const snippet = h.snippet ? ` — ${h.snippet}` : "";
			parts.push(`[${i + 1}] ${h.title}\n    ${h.url}${snippet}`);
		});
	}
	if (payload.sources?.length) {
		const ok = payload.sources.filter((s) => s.status === "ok" && (s.hitCount ?? 0) > 0);
		if (ok.length > 0) {
			parts.push(
				`Sources consulted: ${ok.map((s) => `${s.provider}(${s.hitCount ?? 0} hits, ${s.latencyMs ?? 0}ms)`).join(", ")}.`,
			);
		}
	}
	if (parts.length === 0) return "(no results found across any provider)";
	return parts.join("\n\n");
}

// -------------------------------------------------------------------------
// Tool registration
// -------------------------------------------------------------------------
export const websearchTool: AgentTool<typeof websearchParams> = {
	name: "websearch",
	description:
		"Search the web for current information. Tries DuckDuckGo HTML, then Tavily (if a key is configured), then SearXNG (if a URL is configured), then the DDG Instant Answer API, then Wikipedia. Returns a short summary plus a numbered list of references with title, URL, and snippet. Use this for anything that may be newer than your training data or needs to be verified against a live source.",
	parameters: websearchParams,
	isReadOnly: true,
	execute: async (_id, params, signal): Promise<ToolResult> => {
		const query = params.query.trim();
		if (!query) return errorResult("query must not be empty");
		const numResults = Math.min(params.numResults ?? 6, MAX_RESULTS);
		try {
			const { merged, results } = await runWebsearch(query, {
				numResults,
				recency: params.recency,
				signal,
			});
			const okProvider = results.find((r) => r.status === "ok" && (r.hitCount ?? 0) > 0);
			const text = formatSearchResults(merged, numResults);
			const errored = merged.hits.length === 0;
			return {
				content: [{ type: "text" as const, text }],
				isError: errored,
				details: {
					results: merged.hits,
					sources: merged.sources,
					providerResults: results,
					provider: okProvider?.provider,
				},
			};
		} catch (e) {
			if (signal?.aborted) return errorResult("aborted");
			return errorResult(e instanceof Error ? e.message : String(e));
		}
	},
};

/** Bind a `WebsearchConfig` to the tool — the result behaves identically to
 *  `websearchTool` but pulls the user's Tavily key / SearXNG URL from
 *  `config` rather than the tool defaults. Use this in route layers. */
export function websearchToolWith(config: WebsearchConfig): AgentTool<typeof websearchParams> {
	return {
		...websearchTool,
		execute: async (id, params, signal) => {
			const query = params.query.trim();
			if (!query) return errorResult("query must not be empty");
			const numResults = Math.min(params.numResults ?? 6, MAX_RESULTS);
			try {
				const { merged, results } = await runWebsearch(query, {
					numResults,
					recency: params.recency,
					config,
					signal,
				});
				const okProvider = results.find((r) => r.status === "ok" && (r.hitCount ?? 0) > 0);
				const text = formatSearchResults(merged, numResults);
				const errored = merged.hits.length === 0;
				return {
					content: [{ type: "text" as const, text }],
					isError: errored,
					details: {
						query,
						results: merged.hits,
						answer: merged.answer,
						sources: merged.sources,
						providerResults: results,
						provider: okProvider?.provider,
					},
				};
			} catch (e) {
				if (signal?.aborted) return errorResult("aborted");
				return errorResult(e instanceof Error ? e.message : String(e));
			}
		},
	};
}
