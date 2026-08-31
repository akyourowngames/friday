"use client";

import { ChevronDown, Copy, Globe2 } from "lucide-react";
import type { ToolRun } from "@/lib/types";

/**
 * Compact card for `websearch` tool runs. Renders:
 *  - the query in the header (so it's clear what was searched),
 *  - the synthesized "Summary" line, if any provider returned one,
 *  - a numbered list of references (title, snippet, url),
 *  - a footer listing which providers actually contributed.
 *
 * Falls back to the generic "Output" pre when the tool didn't return
 * structured results (e.g. an error or a single-provider short-circuit).
 */
export function SearchCard({ tool, onToggle }: { tool: ToolRun; onToggle: () => void }) {
	const query = extractQuery(tool.args);
	const results = tool.searchResults ?? [];
	const sources = tool.searchSources ?? [];
	const summary = tool.searchAnswer;
	const hasStructured = Boolean(summary) || results.length > 0;

	const onCopy = (e: React.MouseEvent) => {
		e.stopPropagation();
		if (typeof navigator === "undefined" || !navigator.clipboard) return;
		navigator.clipboard.writeText(tool.output).catch(() => undefined);
	};

	return (
		<section className={`harness-tool-card is-search ${tool.expanded ? "is-open" : ""}`}>
			<button type="button" className="harness-tool-summary" onClick={onToggle} aria-expanded={tool.expanded}>
				<span className={`harness-status-dot is-${tool.status}`} />
				<Globe2 size={15} strokeWidth={1.8} />
				<span className="harness-tool-name">{tool.name}</span>
				<span className="harness-tool-args">{query || "searching…"}</span>
				<ChevronDown size={15} className="harness-tool-chevron" />
			</button>
			{tool.expanded && (
				<div className="harness-tool-detail">
					{hasStructured ? (
						<div className="harness-search-card">
							<div className="harness-search-card-head">
								<span className="harness-search-card-query">
									{query ? `“${query}”` : "Web search"}
								</span>
								<span>
									{results.length} {results.length === 1 ? "result" : "results"}
									{tool.searchProvider ? ` · via ${tool.searchProvider}` : ""}
								</span>
							</div>
							{summary && <p className="harness-search-card-summary">{summary}</p>}
							{results.length > 0 && (
								<ol className="harness-search-card-list">
									{results.map((h, i) => (
										<li className="harness-search-card-item" key={`${h.url}#${i}`}>
											<a
												className="harness-search-card-title"
												href={h.url}
												target="_blank"
												rel="noopener noreferrer"
											>
												<span className="harness-search-card-index">[{i + 1}]</span>
												<span>{h.title || h.url}</span>
											</a>
											<a
												className="harness-search-card-url"
												href={h.url}
												target="_blank"
												rel="noopener noreferrer"
												title={h.url}
											>
												{h.url}
											</a>
											{h.snippet && <p className="harness-search-card-snippet">{h.snippet}</p>}
										</li>
									))}
								</ol>
							)}
							{sources.length > 0 && (
								<div className="harness-search-card-foot">
									Sources consulted:{" "}
									{sources
										.filter((s) => s.status === "ok" && (s.hitCount ?? 0) > 0)
										.map((s) => `${s.provider} (${s.hitCount ?? 0} hits, ${s.latencyMs ?? 0}ms)`)
										.join(", ") || "none"}
								</div>
							)}
						</div>
					) : (
						<div>
							<div className="harness-tool-detail-head">
								<span>Output</span>
								<button type="button" onClick={onCopy} title="Copy output" aria-label="Copy output">
									<Copy size={14} />
								</button>
							</div>
							<pre>{tool.output || "Awaiting tool result…"}</pre>
						</div>
					)}
				</div>
			)}
		</section>
	);
}

function extractQuery(args: unknown): string {
	if (!args || typeof args !== "object") return "";
	const rec = args as Record<string, unknown>;
	const q = rec.query;
	if (typeof q === "string") return q;
	if (Array.isArray(q) && typeof q[0] === "string") return q[0];
	return "";
}
