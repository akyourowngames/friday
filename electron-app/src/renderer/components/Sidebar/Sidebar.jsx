import {
  Plus,
  RefreshCw,
  Search,
  PanelLeftClose,
  PanelLeftOpen,
  MessageSquare,
  Settings,
  Sparkles,
} from "lucide-react";
import { useSessionStore } from "../../stores/sessionStore.js";
import { useSettingsStore } from "../../stores/settingsStore.js";
import { useChatStore } from "../../stores/chatStore.js";
import { SessionList } from "./SessionList.jsx";

export function Sidebar({
  onNewSession,
  onLoadSession,
  onRefresh,
  onRenameSession,
  onDeleteSession,
  activePage = "chat",
  onOpenChat,
  onOpenSkills,
  onOpenSettings,
}) {
  const search = useSessionStore((state) => state.search);
  const setSearch = useSessionStore((state) => state.setSearch);
  const isStreaming = useChatStore((state) => state.isStreaming);
  const collapsed = useSettingsStore((state) => state.sidebarCollapsed);
  const toggleSidebar = useSettingsStore((state) => state.toggleSidebar);

  return (
    <aside className={`sidebar${collapsed ? " collapsed" : ""}`}>
      <div className="sidebar-top">
        <button
          className="sidebar-collapse-btn"
          type="button"
          onClick={toggleSidebar}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
        </button>
        {!collapsed && (
          <>
            <button className="new-session" type="button" onClick={onNewSession}>
              <Plus size={16} />
              <span>New session</span>
              <kbd>Ctrl N</kbd>
            </button>

            <label className="sidebar-search">
              <Search size={15} />
              <input
                value={search}
                placeholder="Search sessions..."
                onChange={(event) => setSearch(event.target.value)}
              />
            </label>
          </>
        )}
      </div>

      {!collapsed && (
        <>
          <section className="sidebar-section">
            <div className="section-title">
              <span>Sessions</span>
              <button
                className="tiny-icon"
                type="button"
                title="Refresh"
                onClick={onRefresh}
              >
                <RefreshCw size={13} />
              </button>
            </div>
            <SessionList
              onLoadSession={onLoadSession}
              onRenameSession={onRenameSession}
              onDeleteSession={onDeleteSession}
              isStreaming={isStreaming}
            />
          </section>
          <nav className="sidebar-nav" aria-label="Ares sections">
            <button className={activePage === "chat" ? "active" : ""} type="button" onClick={onOpenChat}>
              <MessageSquare size={16} /><span>Chat</span>
            </button>
            <button className={activePage === "skills" ? "active" : ""} type="button" onClick={onOpenSkills}>
              <Sparkles size={16} /><span>Skills</span>
            </button>
            <button className={activePage === "settings" ? "active" : ""} type="button" onClick={onOpenSettings}>
              <Settings size={16} /><span>Settings</span>
            </button>
          </nav>
        </>
      )}
    </aside>
  );
}
