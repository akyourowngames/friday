import { Circle, Database, RefreshCw, Zap, Bot, Activity, AlertTriangle } from "lucide-react";
import { useSettingsStore } from "../../stores/settingsStore.js";
import { ContextBar } from "./ContextBar.jsx";

const EXECUTOR_COLORS = {
  idle: "var(--text-faint)",
  scanning: "var(--accent)",
  running: "var(--success)",
  disabled: "var(--text-muted)",
  stopped: "var(--warning)",
};

const EXECUTOR_LABELS = {
  idle: "Executor idle",
  scanning: "Scanning tasks...",
  running: "Executing task...",
  disabled: "Executor off",
  stopped: "Executor stopped",
};

export function StatusBar({ onRetry }) {
  const connected = useSettingsStore((state) => state.connected);
  const model = useSettingsStore((state) => state.model);
  const memoryCount = useSettingsStore((state) => state.memoryCount);
  const taskCount = useSettingsStore((state) => state.taskCount);
  const autoExecCount = useSettingsStore((state) => state.autoExecCount);
  const executorState = useSettingsStore((state) => state.executorState);
  const executorCurrentTask = useSettingsStore((state) => state.executorCurrentTask);
  const executorTasksFailed = useSettingsStore((state) => state.executorTasksFailed);
  const lastError = useSettingsStore((state) => state.lastError);

  const execColor = EXECUTOR_COLORS[executorState] || "var(--text-muted)";
  const execLabel = EXECUTOR_LABELS[executorState] || `Executor: ${executorState}`;

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
      <div>
        <span>{taskCount} tasks</span>
      </div>
      {autoExecCount > 0 ? (
        <div className="status-auto-exec" title={executorCurrentTask ? `Executing: ${executorCurrentTask}` : "Tasks pending auto-execution"}>
          <Bot size={13} />
          <span>{autoExecCount} auto</span>
        </div>
      ) : null}
      <div style={{ color: execColor }} title={executorCurrentTask ? `Current task: ${executorCurrentTask}` : execLabel}>
        {executorState === "running" || executorState === "scanning" ? (
          <Activity size={12} />
        ) : executorState === "disabled" || executorState === "stopped" ? (
          <Circle size={9} />
        ) : executorTasksFailed > 0 ? (
          <AlertTriangle size={12} />
        ) : (
          <Circle size={9} />
        )}
        <span style={{ fontSize: "11px" }}>{execLabel}</span>
      </div>
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
