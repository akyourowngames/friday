import { RotateCcw, X } from "lucide-react";
import useTerminalStore from "../stores/terminalStore";

export default function TerminalHeader() {
  const { sessionId, isConnected, killTerminal, closePanel } = useTerminalStore();

  const handleClose = () => {
    killTerminal();
    closePanel();
  };

  return (
    <div className="terminal-header">
      <div className="terminal-header-left">
        <span className="terminal-dot" />
        <span className="terminal-title">Terminal</span>
        {isConnected && sessionId && (
          <span className="terminal-session-id">{sessionId}</span>
        )}
      </div>
      <div className="terminal-header-right">
        {!isConnected && (
          <button
            className="terminal-restart-btn"
            onClick={() => useTerminalStore.getState().createTerminal()}
            title="Restart terminal"
          >
            <RotateCcw size={13} strokeWidth={2.2} />
          </button>
        )}
        <button
          className="terminal-close-btn"
          onClick={handleClose}
          title="Close terminal"
        >
          <X size={14} strokeWidth={2.2} />
        </button>
      </div>
    </div>
  );
}
