import React, { useEffect } from "react";
import { Settings } from "lucide-react";
import { ChatArea } from "./components/Chat/ChatArea.jsx";
import { SettingsPanel } from "./components/Settings/SettingsPanel.jsx";
import { Sidebar } from "./components/Sidebar/Sidebar.jsx";
import { StatusBar } from "./components/common/StatusBar.jsx";
import { TaskNotification } from "./components/common/TaskNotification.jsx";
import TerminalPanel from "./components/TerminalPanel.jsx";
import TerminalHeader from "./components/TerminalHeader.jsx";
import useTerminalStore from "./stores/terminalStore.js";
import { useWebSocket } from "./hooks/useWebSocket.js";
import { useSettingsStore } from "./stores/settingsStore.js";
import { useChatStore } from "./stores/chatStore.js";

export default function App() {
  const connection = useWebSocket();
  const settingsOpen = useSettingsStore((state) => state.settingsOpen);
  const setSettingsOpen = useSettingsStore((state) => state.setSettingsOpen);
  const isTerminalOpen = useTerminalStore((state) => state.isOpen);
  const sidebarCollapsed = useSettingsStore((state) => state.sidebarCollapsed);
  const [splitPos, setSplitPos] = React.useState(60);
  const isDragging = React.useRef(false);

  const handleDividerMouseDown = (e) => {
    isDragging.current = true;
    e.preventDefault();
  };

  React.useEffect(() => {
    const handleMouseMove = (e) => {
      if (!isDragging.current) return;
      const container = document.querySelector(".terminal-split-layout");
      if (!container) return;
      const rect = container.getBoundingClientRect();
      const pct = ((e.clientX - rect.left) / rect.width) * 100;
      setSplitPos(Math.min(Math.max(pct, 30), 80));
    };

    const handleMouseUp = () => {
      isDragging.current = false;
    };

    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);
    return () => {
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
    };
  }, []);

  // Keyboard shortcut: Ctrl+` to toggle terminal
  React.useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.ctrlKey && e.key === "`") {
        e.preventDefault();
        useTerminalStore.getState().togglePanel();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  // Wire WebSocket terminal:exec messages to terminal
  useEffect(() => {
    const handleWsMessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === "terminal:exec") {
          const store = useTerminalStore.getState();
          store.handleExecCommand(msg.command, msg.cmd_id);
        }
      } catch (err) {
        // Not JSON or not our message
      }
    };

    const ws = useChatStore.getState()?.websocket;
    if (ws) {
      ws.addEventListener("message", handleWsMessage);
      return () => ws.removeEventListener("message", handleWsMessage);
    }
  }, []);

  useEffect(() => {
    function onKeyDown(e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "n") {
        e.preventDefault();
        connection.newSession();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [connection.newSession]);

  return (
    <div className={`app-shell${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
      <Sidebar
        onNewSession={connection.newSession}
        onLoadSession={connection.loadSession}
        onRefresh={connection.refreshSidebar}
        onRenameSession={connection.renameSession}
        onDeleteSession={connection.deleteSession}
      />
      <main className="main-pane">
        <header className="top-bar">
          <div className="top-title">
            <span>Ares</span>
            <small>{connection.connected ? "Connected" : "Reconnecting"}</small>
          </div>
          <div className="top-bar-actions">
            <button
              className="icon-button"
              type="button"
              aria-label="Open settings"
              title="Settings"
              onClick={() => setSettingsOpen(true)}
            >
              <Settings size={18} strokeWidth={2.2} />
            </button>
          </div>
        </header>
        {isTerminalOpen ? (
          <div className="terminal-split-layout">
            <div className="chat-panel" style={{ width: `${splitPos}%` }}>
              <ChatArea onSend={connection.sendMessage} />
              <TaskNotification />
            </div>
            <div
              className="split-divider"
              onMouseDown={handleDividerMouseDown}
            />
            <div className="terminal-panel">
              <TerminalHeader />
              <TerminalPanel />
            </div>
          </div>
        ) : (
          <>
            <ChatArea onSend={connection.sendMessage} />
            <TaskNotification />
          </>
        )}
        <StatusBar onRetry={connection.reconnect} />
      </main>
      {settingsOpen ? (
        <SettingsPanel
          onClose={() => setSettingsOpen(false)}
          onSetModel={connection.setModel}
          onRefresh={connection.refreshSidebar}
        />
      ) : null}
    </div>
  );
}
