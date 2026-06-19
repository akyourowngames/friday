import { ExternalLink } from "lucide-react";

function normalizeResults(content) {
  if (!content) {
    return [];
  }
  if (Array.isArray(content)) {
    return content;
  }
  return content.results || content.items || [];
}

export function WebSearchCard({ args = {}, content }) {
  const results = normalizeResults(content);
  const summary = content?.summary || content?.answer || content?.content_summary || "";
  const query = args.query || content?.query || "web search";

  return (
    <div className="tool-body">
      <div className="tool-query">{query}</div>
      {summary ? <p className="tool-summary">{summary}</p> : null}
      <div className="search-results">
        {results.slice(0, 5).map((result, index) => (
          <a
            className="search-result"
            href={result.url || result.href}
            target="_blank"
            rel="noreferrer"
            key={`${result.url || result.title || index}`}
          >
            <span className="result-index">{index + 1}</span>
            <span className="result-copy">
              <strong>{result.title || result.url || "Untitled result"}</strong>
              <small>{result.snippet || result.content || result.description || ""}</small>
            </span>
            <ExternalLink size={14} />
          </a>
        ))}
      </div>
    </div>
  );
}
