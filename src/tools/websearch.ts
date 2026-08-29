/**
 * Real web search for friday-ng.
 *
 * Uses the DuckDuckGo Instant Answer API (keyless, JSON, no scraping) to
 * return the top abstract plus related result links for a query. Falls back
 * to Wikipedia's opensearch API when DuckDuckGo has no instant answer.
 */
import { Type } from "typebox";
import type { AgentTool, ToolResult } from "../types.ts";

const WEBSEARCH_TIMEOUT_MS = 15_000;
const MAX_RESULTS = 8;
const USER_AGENT = "friday-ng/0.2 (websearch tool)";

const websearchParams = Type.Object({
	query: Type.String({ description: "Search query" }),
	numResults: Type.Optional(
		Type.Integer({ description: "Maximum related results to return (default 5)", minimum: 1, maximum: MAX_RESULTS }),
	),
});

interface SearchHit {
	title: string;
	url: string;
	snippet: string;
}

interface SearchPayload {
	heading?: string;
	abstract?: string;
	abstractUrl?: string;
	answer?: string;
	hits: SearchHit[];
	source: "duckduckgo" | "wikipedia";
}

function errorResult(message: string): ToolResult {
	return {
		content: [{ type: "text" as const, text: `Error: ${message}` }],
		details: { error: true },
		isError: true,
	};
}

/** Query the DuckDuckGo Instant Answer API. */
async function searchDuckDuckGo(query: string, signal?: AbortSignal): Promise<SearchPayload | undefined> {
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
			if (Array.isArray(t?.Topics)) {
				walkTopics(t.Topics);
			} else if (t?.FirstURL && t?.Text) {
				const text: string = t.Text;
				// DDG related-topic text is "<title> - <source><snippet>" —
				// split the title off at the first separator for readability.
				const title = text.split(/ - | – |: /, 1)[0] || text.slice(0, 80);
				hits.push({
					title,
					url: t.FirstURL as string,
					snippet: text.length > 240 ? `${text.slice(0, 237)}…` : text,
				});
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
		abstract: hasAbstract ? abstract : undefined,
		abstractUrl: hasAbstract ? (data.AbstractURL as string) : undefined,
		answer: typeof data?.Answer === "string" && data.Answer.length > 0 ? data.Answer : undefined,
		hits,
		source: "duckduckgo",
	};
}

/** Fallback: Wikipedia opensearch (title + link list). */
async function searchWikipedia(query: string, signal?: AbortSignal): Promise<SearchPayload> {
	const url =
		`https://en.wikipedia.org/w/api.php?action=opensearch` +
		`&search=${encodeURIComponent(query)}&limit=${MAX_RESULTS}&namespace=0&format=json`;
	const res = await fetch(url, {
		headers: { "User-Agent": USER_AGENT },
		signal: signal ?? AbortSignal.timeout(WEBSEARCH_TIMEOUT_MS),
	});
	if (!res.ok) throw new Error(`Wikipedia returned HTTP ${res.status}`);
	const data: any = await res.json();
	// opensearch: [query, [titles], [descriptions], [urls]]
	const titles: string[] = data?.[1] ?? [];
	const urls: string[] = data?.[3] ?? [];
	const descriptions: string[] = data?.[2] ?? [];
	const hits: SearchHit[] = [];
	for (let i = 0; i < titles.length; i++) {
		if (!urls[i]) continue;
		hits.push({
			title: titles[i],
			url: urls[i],
			snippet: descriptions[i] ?? "",
		});
	}
	return { hits, source: "wikipedia" };
}

/** Format a search payload as readable text for the model. */
export function formatSearchResults(payload: SearchPayload, numResults: number): string {
	const parts: string[] = [];
	if (payload.answer) parts.push(`Answer: ${payload.answer}`);
	if (payload.abstract) {
		const src = payload.abstractUrl ? ` (source: ${payload.abstractUrl})` : "";
		const heading = payload.heading ? `${payload.heading}: ` : "";
		parts.push(`${heading}${payload.abstract}${src}`);
	}
	if (payload.hits.length > 0) {
		parts.push("Results:");
		for (let i = 0; i < Math.min(numResults, payload.hits.length); i++) {
			const h = payload.hits[i]!;
			parts.push(`${i + 1}. ${h.title}\n   ${h.url}${h.snippet ? `\n   ${h.snippet}` : ""}`);
		}
	}
	if (parts.length === 0) return "(no results found)";
	return parts.join("\n\n");
}

export const websearchTool: AgentTool<typeof websearchParams> = {
	name: "websearch",
	description:
		"Search the web for current information. Returns an abstract/answer plus related result titles, snippets, and URLs. Use this for anything that may be newer than your training data.",
	parameters: websearchParams,
	isReadOnly: true,
	execute: async (_id, params, signal): Promise<ToolResult> => {
		const query = params.query.trim();
		if (!query) return errorResult("query must not be empty");
		const numResults = Math.min(params.numResults ?? 5, MAX_RESULTS);
		try {
			let payload: SearchPayload | undefined;
			try {
				payload = await searchDuckDuckGo(query, signal);
			} catch (e) {
				if (signal?.aborted) return errorResult("aborted");
				// DDG unreachable/empty — fall through to Wikipedia.
				payload = undefined;
			}
			if (!payload || (payload.hits.length === 0 && !payload.abstract && !payload.answer)) {
				payload = await searchWikipedia(query, signal);
			}
			return {
				content: [{ type: "text" as const, text: formatSearchResults(payload, numResults) }],
				details: { source: payload.source, results: payload.hits.length },
			};
		} catch (e) {
			if (signal?.aborted) return errorResult("aborted");
			return errorResult(e instanceof Error ? e.message : String(e));
		}
	},
};
