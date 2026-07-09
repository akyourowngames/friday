import { useState, useEffect } from "react";
import { useSettingsStore } from "../../stores/settingsStore.js";

function formatTokens(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return `${n}`;
}

export function ContextBar() {
  const contextUsage = useSettingsStore((state) => state.contextUsage);
  const [animPercent, setAnimPercent] = useState(0);
  const [pulse, setPulse] = useState(false);
  const [showBreakdown, setShowBreakdown] = useState(false);

  useEffect(() => {
    const raw = Math.min(contextUsage.percent, 100);
    const display = raw < 1 && raw > 0 ? Math.max(raw, 1.5) : raw;
    const timer = setTimeout(() => setAnimPercent(display), 50);
    return () => clearTimeout(timer);
  }, [contextUsage.percent]);

  useEffect(() => {
    if (contextUsage.percent > 80) {
      setPulse(true);
      const t = setTimeout(() => setPulse(false), 600);
      return () => clearTimeout(t);
    }
  }, [contextUsage.percent]);

  const color =
    contextUsage.percent > 90 ? "var(--danger, #ef4444)" :
    contextUsage.percent > 70 ? "var(--warning, #eab308)" :
    "var(--success, #22c55e)";

  const bd = contextUsage.breakdown || {};
  const totalTokens = contextUsage.total || 128000;

  // Breakdown segment widths as % of total context window
  const sysPct = Math.min((bd.system_prompt || 0) / totalTokens * 100, 100);
  const histPct = Math.min((bd.history || 0) / totalTokens * 100, 100);
  const toolPct = Math.min((bd.tool_output || 0) / totalTokens * 100, 100);

  return (
    <div
      className="context-bar"
      title={`${contextUsage.percent}% of context used (${formatTokens(contextUsage.used)} / ${formatTokens(totalTokens)})`}
      onClick={() => setShowBreakdown(!showBreakdown)}
      style={{ cursor: "pointer" }}
    >
      <span className="context-bar-label">CTX</span>
      <div className="context-bar-track">
        {/* Stacked segments: system prompt → history → tool output */}
        {sysPct + histPct + toolPct > 0 && (
          <div
            className="context-bar-fill-segments"
            style={{
              width: `${animPercent}%`,
              display: "flex",
            }}
          >
            {sysPct > 0 && (
              <div
                style={{
                  width: `${(sysPct / Math.max(sysPct + histPct + toolPct, 1)) * 100}%`,
                  backgroundColor: "var(--accent, #6366f1)",
                  height: "100%",
                  minWidth: sysPct > 1 ? undefined : 0,
                }}
                title={`System prompt: ${formatTokens(bd.system_prompt || 0)}`}
              />
            )}
            {histPct > 0 && (
              <div
                style={{
                  width: `${(histPct / Math.max(sysPct + histPct + toolPct, 1)) * 100}%`,
                  backgroundColor: color,
                  height: "100%",
                }}
                title={`History: ${formatTokens(bd.history || 0)}`}
              />
            )}
            {toolPct > 0 && (
              <div
                style={{
                  width: `${(toolPct / Math.max(sysPct + histPct + toolPct, 1)) * 100}%`,
                  backgroundColor: "var(--warning, #eab308)",
                  height: "100%",
                  opacity: 0.7,
                }}
                title={`Tool output: ${formatTokens(bd.tool_output || 0)}`}
              />
            )}
          </div>
        )}
        {/* Fallback: plain fill if no breakdown */}
        {sysPct + histPct + toolPct === 0 && (
          <div
            className={`context-bar-fill${pulse ? " pulse" : ""}`}
            style={{
              width: `${animPercent}%`,
              backgroundColor: color,
            }}
          />
        )}
      </div>
      <span className="context-bar-value" style={{ color }}>
        {contextUsage.percent}%
      </span>

      {/* Breakdown tooltip on click */}
      {showBreakdown && (
        <div
          style={{
            position: "absolute",
            bottom: "100%",
            left: 0,
            background: "var(--surface, #1e1e2e)",
            border: "1px solid var(--border, #333)",
            borderRadius: 6,
            padding: "8px 10px",
            fontSize: 11,
            lineHeight: 1.6,
            whiteSpace: "nowrap",
            zIndex: 100,
            boxShadow: "0 4px 12px rgba(0,0,0,0.4)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <div style={{ fontWeight: 600, marginBottom: 4 }}>
            {formatTokens(contextUsage.used)} / {formatTokens(totalTokens)} tokens
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: "var(--accent, #6366f1)", display: "inline-block" }} />
            <span>System: {formatTokens(bd.system_prompt || 0)}</span>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: color, display: "inline-block" }} />
            <span>History: {formatTokens(bd.history || 0)}</span>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: "var(--warning, #eab308)", opacity: 0.7, display: "inline-block" }} />
            <span>Tools: {formatTokens(bd.tool_output || 0)}</span>
          </div>
        </div>
      )}
    </div>
  );
}
