import { X, RefreshCw, Server, Cpu, Database, CheckCircle2 } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore.js";
import { ModelSelector } from "./ModelSelector.jsx";

export function SettingsPanel({ onClose, onSetModel, onRefresh }) {
  const model = useSettingsStore((s) => s.model);
  const memoryCount = useSettingsStore((s) => s.memoryCount);
  const taskCount = useSettingsStore((s) => s.taskCount);
  const serverUrl = useSettingsStore((s) => s.serverUrl);

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
          </section>

          <section className="settings-section">
            <h3 className="settings-section-title">
              <Database size={14} />
              Status
            </h3>
            <div className="settings-stats">
              <div className="settings-stat">
                <span className="settings-stat-label">Memories</span>
                <span className="settings-stat-value">{memoryCount}</span>
              </div>
              <div className="settings-stat">
                <span className="settings-stat-label">Tasks</span>
                <span className="settings-stat-value">{taskCount}</span>
              </div>
              <div className="settings-stat">
                <span className="settings-stat-label">Model</span>
                <span className="settings-stat-value settings-stat-model">{model}</span>
              </div>
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
