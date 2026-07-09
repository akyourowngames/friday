import { Circle, Database, Hash, RefreshCw, Zap } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore.js";
import { useSessionStore } from "../../stores/sessionStore.js";
import { ContextBar } from "./ContextBar.jsx";


export function StatusBar({ onRetry }) {
  const connected = useSettingsStore((state) => state.connected);
  const model = useSettingsStore((state) => state.model);
  const memoryCount = useSettingsStore((state) => state.memoryCount);
  const lastError = useSettingsStore((state) => state.lastError);
  const activeSessionId = useSessionStore((state) => state.activeSessionId);


  return (
    <footer className="status-bar">
      <div className={connected ? "status-online" : "status-offline"}>
        <Circle size={9} fill="currentColor" />
        <span>{connected ? "Gateway ready" : "Reconnecting"}</span>
      </div>
      <div>
        <Zap size={13} />
        <span>{model}</span>
      </div>
      <div>
        <Database size={13} />
        <span>{memoryCount} memories</span>
      </div>
      {activeSessionId ? (
        <div>
          <Hash size={13} />
          <span>session {activeSessionId}</span>
        </div>
      ) : null}

      {lastError ? (
        <button className="status-retry" type="button" onClick={onRetry}>
          <RefreshCw size={12} />
          <span>{lastError}</span>
        </button>
      ) : null}
      <ContextBar />
    </footer>
  );
}
