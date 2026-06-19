import { useState, useRef, useEffect, useMemo } from "react";
import { ChevronDown, Search, Check, Sparkles, Brain, Cpu, Zap } from "lucide-react";
import { MODEL_REGISTRY, useSettingsStore } from "../../stores/settingsStore.js";

const GROUP_ICONS = {
  free: <Sparkles size={13} />,
  claude: <Brain size={13} />,
  gpt: <Cpu size={13} />,
  gemini: <Zap size={13} />,
  other: <Cpu size={13} />,
};

export function ModelSelector({ onSetModel }) {
  const currentId = useSettingsStore((s) => s.model);
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const ref = useRef(null);
  const inputRef = useRef(null);

  const current = useMemo(() => {
    for (const g of Object.values(MODEL_REGISTRY)) {
      const m = g.models.find((m) => m.id === currentId);
      if (m) return m;
    }
    return null;
  }, [currentId]);

  useEffect(() => {
    function outside(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, []);

  useEffect(() => {
    if (open && inputRef.current) inputRef.current.focus();
  }, [open]);

  const q = search.toLowerCase();
  const groups = useMemo(() => {
    const out = {};
    for (const [k, g] of Object.entries(MODEL_REGISTRY)) {
      const filtered = g.models.filter(
        (m) =>
          !q ||
          m.label.toLowerCase().includes(q) ||
          m.id.toLowerCase().includes(q) ||
          m.provider.toLowerCase().includes(q)
      );
      if (filtered.length) out[k] = { ...g, models: filtered };
    }
    return out;
  }, [q]);

  function pick(id) {
    onSetModel(id);
    setOpen(false);
    setSearch("");
  }

  return (
    <div className="model-selector" ref={ref}>
      <button
        type="button"
        className="model-selector-trigger"
        onClick={() => setOpen(!open)}
      >
        <div className="model-selector-left">
          {current ? (
            <>
              <span className="model-selector-badge">{current.provider}</span>
              <span className="model-selector-label">{current.label}</span>
            </>
          ) : (
            <span className="model-selector-label model-selector-label--dim">{currentId}</span>
          )}
        </div>
        <ChevronDown size={14} className={`model-selector-chevron${open ? " open" : ""}`} />
      </button>

      {open && (
        <div className="model-selector-dropdown">
          <div className="model-selector-search">
            <Search size={14} />
            <input
              ref={inputRef}
              placeholder="Search models..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>

          <div className="model-selector-list">
            {Object.entries(groups).map(([k, g]) => (
              <div key={k} className="model-selector-group">
                <div className="model-selector-group-label">
                  {GROUP_ICONS[k]}
                  {g.label}
                </div>
                {g.models.map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    className={`model-selector-item${m.id === currentId ? " active" : ""}`}
                    onClick={() => pick(m.id)}
                  >
                    <span className="model-selector-item-label">{m.label}</span>
                    <span className="model-selector-item-meta">
                      <span className="model-selector-item-provider">{m.provider}</span>
                      {m.id === currentId && <Check size={13} className="model-selector-check" />}
                    </span>
                  </button>
                ))}
              </div>
            ))}
            {Object.keys(groups).length === 0 && (
              <div className="model-selector-empty">No models found</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
