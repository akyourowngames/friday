import {
  Archive,
  Bot,
  CalendarClock,
  LayoutDashboard,
  MessageCircle,
  Plus,
  RefreshCw,
  Search,
  Sparkles
} from "lucide-react";
import { useSessionStore } from "../../stores/sessionStore.js";
import { useSettingsStore } from "../../stores/settingsStore.js";
import { SessionList } from "./SessionList.jsx";

export function Sidebar({ onNewSession, onLoadSession, onRefresh }) {
  const search = useSessionStore((state) => state.search);
  const setSearch = useSessionStore((state) => state.setSearch);
  const memoryCount = useSettingsStore((state) => state.memoryCount);
  const taskCount = useSettingsStore((state) => state.taskCount);

  return (
    <aside className="sidebar">
      <div className="sidebar-top">
        <button className="new-session" type="button" onClick={onNewSession}>
          <Plus size={16} />
          <span>New session</span>
          <kbd>Ctrl N</kbd>
        </button>
        <nav className="sidebar-nav" aria-label="Ares navigation">
          <button type="button">
            <Sparkles size={16} />
            <span>Skills & Tools</span>
          </button>
          <button type="button">
            <MessageCircle size={16} />
            <span>Messaging</span>
          </button>
          <button type="button">
            <Archive size={16} />
            <span>Artifacts</span>
          </button>
          <button type="button">
            <LayoutDashboard size={16} />
            <span>Dashboard</span>
          </button>
        </nav>
        <label className="sidebar-search">
          <Search size={15} />
          <input
            value={search}
            placeholder="Search sessions..."
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
      </div>

      <section className="sidebar-section">
        <div className="section-title">
          <span>Sessions</span>
          <button className="tiny-icon" type="button" title="Refresh" onClick={onRefresh}>
            <RefreshCw size={13} />
          </button>
        </div>
        <SessionList onLoadSession={onLoadSession} />
      </section>

      <section className="sidebar-section compact-section">
        <div className="section-title">
          <span>Cron jobs</span>
          <small>{taskCount}</small>
        </div>
        <div className="task-pill">
          <CalendarClock size={14} />
          <span>{taskCount} pending tasks</span>
        </div>
        <div className="task-pill">
          <Bot size={14} />
          <span>{memoryCount} memories</span>
        </div>
      </section>
    </aside>
  );
}
