import { Settings } from "lucide-react";
import { ChatArea } from "./components/Chat/ChatArea.jsx";
import { SettingsPanel } from "./components/Settings/SettingsPanel.jsx";
import { Sidebar } from "./components/Sidebar/Sidebar.jsx";
import { StatusBar } from "./components/common/StatusBar.jsx";
import { useWebSocket } from "./hooks/useWebSocket.js";
import { useSettingsStore } from "./stores/settingsStore.js";

export default function App() {
  const connection = useWebSocket();
  const settingsOpen = useSettingsStore((state) => state.settingsOpen);
  const setSettingsOpen = useSettingsStore((state) => state.setSettingsOpen);

  return (
    <div className="app-shell">
      <Sidebar
        onNewSession={connection.newSession}
        onLoadSession={connection.loadSession}
        onRefresh={connection.refreshSidebar}
      />
      <main className="main-pane">
        <header className="top-bar">
          <div className="top-title">
            <span>Ares Desktop</span>
            <small>{connection.connected ? "Connected" : "Reconnecting"}</small>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="Open settings"
            title="Settings"
            onClick={() => setSettingsOpen(true)}
          >
            <Settings size={17} />
          </button>
        </header>
        <ChatArea onSend={connection.sendMessage} />
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
