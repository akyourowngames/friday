import { Database, KeyRound, RotateCw, X } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore.js";
import { ModelSelector } from "./ModelSelector.jsx";

export function SettingsPanel({ onClose, onSetModel, onRefresh }) {
  const model = useSettingsStore((state) => state.model);
  const memoryCount = useSettingsStore((state) => state.memoryCount);
  const taskCount = useSettingsStore((state) => state.taskCount);
  const serverUrl = useSettingsStore((state) => state.serverUrl);

  return (
    <div className="settings-scrim" role="presentation" onMouseDown={onClose}>
      <aside className="settings-panel" role="dialog" aria-label="Settings" onMouseDown={(e) => e.stopPropagation()}>
        <header>
          <div>
            <h2>Settings</h2>
            <p>Runtime controls for this Ares desktop session.</p>
          </div>
          <button className="icon-button" type="button" aria-label="Close settings" onClick={onClose}>
            <X size={17} />
          </button>
        </header>

        <div className="settings-group">
          <ModelSelector onSetModel={onSetModel} />
          <label className="field">
            <span>
              <KeyRound size={15} />
              Server
            </span>
            <input value={serverUrl || "ws://127.0.0.1:8765"} readOnly />
          </label>
        </div>

        <div className="settings-metrics">
          <div>
            <Database size={16} />
            <span>Memories</span>
            <strong>{memoryCount}</strong>
          </div>
          <div>
            <Database size={16} />
            <span>Tasks</span>
            <strong>{taskCount}</strong>
          </div>
          <div>
            <Database size={16} />
            <span>Model</span>
            <strong>{model}</strong>
          </div>
        </div>

        <button className="primary-button" type="button" onClick={onRefresh}>
          <RotateCw size={16} />
          Refresh desktop state
        </button>
      </aside>
    </div>
  );
}
