import React, { useEffect } from "react";
import { MessageSquare, Settings } from "lucide-react";
import { ChatArea } from "./components/Chat/ChatArea.jsx";
import { SettingsPage } from "./components/Settings/SettingsPage.jsx";
import { SkillsPage } from "./components/Skills/SkillsPage.jsx";
import { OnboardingPage } from "./components/Onboarding/OnboardingPage.jsx";
import { Sidebar } from "./components/Sidebar/Sidebar.jsx";
import { StatusBar } from "./components/common/StatusBar.jsx";
import TerminalPanel from "./components/Terminal/TerminalPanel.jsx";
import TerminalHeader from "./components/Terminal/TerminalHeader.jsx";
import useTerminalStore from "./stores/terminalStore.js";
import { useWebSocket } from "./hooks/useWebSocket.js";
import { useSettingsStore } from "./stores/settingsStore.js";
import { aresSocket } from "./lib/websocket.js";

export default function App() {
  const connection = useWebSocket();
  const settingsOpen = useSettingsStore((state) => state.settingsOpen);
  const setSettingsOpen = useSettingsStore((state) => state.setSettingsOpen);
  const [activePage, setActivePage] = React.useState(settingsOpen ? "settings" : "chat");
  const isTerminalOpen = useTerminalStore((state) => state.isOpen);
  const sidebarCollapsed = useSettingsStore((state) => state.sidebarCollapsed);
  const onboardingLoaded = useSettingsStore((state) => state.onboardingLoaded);
  const onboardingCompleted = useSettingsStore((state) => state.onboardingCompleted);
  const [splitPos, setSplitPos] = React.useState(60);
  const isDragging = React.useRef(false);

  const openPage = React.useCallback((page) => {
    setActivePage(page);
    setSettingsOpen(page === "settings");
  }, [setSettingsOpen]);

  const startNewSession = React.useCallback(() => {
    connection.newSession();
    openPage("chat");
  }, [connection.newSession, openPage]);

  const loadSession = React.useCallback((sessionId) => {
    connection.loadSession(sessionId);
    openPage("chat");
  }, [connection.loadSession, openPage]);

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
    const off = aresSocket.on("terminal:exec", (msg) => {
      const store = useTerminalStore.getState();
      store.handleExecCommand(msg.command, msg.cmd_id, aresSocket.ws);
    });
    return off;
  }, []);

  useEffect(() => {
    function onKeyDown(e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "n") {
        e.preventDefault();
        startNewSession();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [startNewSession]);

  if (onboardingLoaded && !onboardingCompleted) {
    return <OnboardingPage onComplete={connection.completeOnboarding} />;
  }

  return (
    <div className={`app-shell${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
      <Sidebar
        onNewSession={startNewSession}
        onLoadSession={loadSession}
        onRefresh={connection.refreshSidebar}
        onRenameSession={connection.renameSession}
        onDeleteSession={connection.deleteSession}
        activePage={activePage}
        onOpenSettings={() => openPage("settings")}
        onOpenSkills={() => openPage("skills")}
        onOpenChat={() => openPage("chat")}
      />
      <main className="main-pane">
        <header className="top-bar">
          <div className="top-title">
            <span>{activePage === "settings" ? "Settings" : activePage === "skills" ? "Skills" : "Ares"}</span>
            <small>{connection.connected ? "Connected" : "Reconnecting"}</small>
          </div>
          <div className="top-bar-actions">
            <button
              className="icon-button"
              type="button"
              aria-label={activePage !== "chat" ? "Back to chat" : "Open settings"}
              title={activePage !== "chat" ? "Back to chat" : "Settings"}
              onClick={() => openPage(activePage === "chat" ? "settings" : "chat")}
            >
              {activePage !== "chat" ? (
                <MessageSquare size={18} strokeWidth={2.2} />
              ) : (
                <Settings size={18} strokeWidth={2.2} />
              )}
            </button>
          </div>
        </header>
        {activePage === "settings" ? (
          <SettingsPage
            onBack={() => openPage("chat")}
            onSetModel={connection.setModel}
            onRefresh={connection.refreshSidebar}
            onFetchPersonalSettings={connection.fetchPersonalSettings}
            onSavePersonalSettings={connection.savePersonalSettings}
          />
        ) : activePage === "skills" ? (
          <SkillsPage />
        ) : isTerminalOpen ? (
          <div className="terminal-split-layout">
            <div className="chat-panel" style={{ width: `${splitPos}%` }}>
              <ChatArea onSend={connection.sendMessage} />
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
          </>
        )}
        <StatusBar onRetry={connection.reconnect} />
      </main>
    </div>
  );
}
