import React, { useState } from "react";
import { Trash2, Copy, X, Check } from "lucide-react";
import useTerminalStore from "../../stores/terminalStore";

/**
 * Terminal panel header bar with status indicator and clean action buttons.
 */
export default function TerminalHeader() {
  const { isConnected, killTerminal, closePanel, lastSelection } = useTerminalStore();
  const [copied, setCopied] = useState(false);

  const handleClose = () => {
    killTerminal();
    closePanel();
  };

  const handleClear = () => {
    window.dispatchEvent(new CustomEvent("terminal:clear"));
  };

  const handleCopy = async () => {
    if (lastSelection?.text) {
      try {
        await navigator.clipboard.writeText(lastSelection.text);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      } catch (err) {
        console.error("Failed to copy text: ", err);
      }
    }
  };

  return (
    <div className="terminal-header">
      <div className="terminal-header-left">
        <span className={`terminal-status-dot ${isConnected ? "active" : "offline"}`} />
        <span className="terminal-title">Terminal</span>
      </div>

      <div className="terminal-header-right">
        {isConnected && (
          <>
            <button
              className="terminal-action-btn"
              onClick={handleClear}
              title="Clear terminal buffer"
            >
              <Trash2 size={13} strokeWidth={2.2} />
            </button>

            <button
              className={`terminal-action-btn ${lastSelection ? "active" : "disabled"}`}
              onClick={handleCopy}
              disabled={!lastSelection}
              title={lastSelection ? "Copy selection" : "No selection to copy"}
            >
              {copied ? (
                <Check size={13} strokeWidth={2.2} className="success-icon" />
              ) : (
                <Copy size={13} strokeWidth={2.2} />
              )}
            </button>
          </>
        )}

        <button
          className="terminal-action-btn close"
          onClick={handleClose}
          title="Close terminal"
        >
          <X size={14} strokeWidth={2.2} />
        </button>
      </div>
    </div>
  );
}
