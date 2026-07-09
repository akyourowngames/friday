import { X, RefreshCw, Server, Cpu, Database, Wifi, WifiOff } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore.js";
import { ModelSelector } from "./ModelSelector.jsx";

function memoryText(memory) {
  if (!memory) {
    return "";
  }
  return memory.fact_text || memory.content || memory.text || JSON.stringify(memory);
}

export function SettingsPanel({ onClose, onSetModel, onRefresh }) {
  const memories = useSettingsStore((s) => s.memories);
  const serverUrl = useSettingsStore((s) => s.serverUrl);
  const connected = useSettingsStore((s) => s.connected);

  return (
    <div className="settings-scrim" onClick={onClose}>
      <aside className="settings-panel" onClick={(e) => e.stopPropagation()}>
        <header className="settings-header">
          <h2>Settings</h2>
          <button className="settings-close" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </header>

        <div className="settings-body">
          <section className="settings-section">
            <h3 className="settings-section-title">
              <Cpu size={14} />
              Model
            </h3>
            <ModelSelector onSetModel={onSetModel} />
          </section>

          <section className="settings-section">
            <h3 className="settings-section-title">
              <Server size={14} />
              Server
            </h3>
            <div className="settings-field">
              <input
                className="settings-input"
                value={serverUrl || "ws://127.0.0.1:8765"}
                readOnly
              />
            </div>
            <div className={`settings-connection ${connected ? "online" : "offline"}`}>
              {connected ? <Wifi size={13} /> : <WifiOff size={13} />}
              <span>{connected ? "Connected to Ares server" : "Waiting for Ares server"}</span>
            </div>
          </section>

          <section className="settings-section">
            <h3 className="settings-section-title">
              <Database size={14} />
              Recent memories
            </h3>
            <div className="settings-memory-list">
              {memories.slice(0, 5).map((memory, index) => (
                <div className="settings-memory-row" key={memory.fact_id || memory.id || index}>
                  <span>{memoryText(memory)}</span>
                </div>
              ))}
              {!memories.length ? (
                <div className="settings-empty">No memories stored yet</div>
              ) : null}
            </div>
          </section>

          <button className="settings-refresh" onClick={onRefresh}>
            <RefreshCw size={15} />
            Refresh state
          </button>
        </div>
      </aside>
    </div>
  );
}
