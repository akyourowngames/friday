import { useSessionStore } from "../../stores/sessionStore.js";
import { SessionItem } from "./SessionItem.jsx";

export function SessionList({ onLoadSession, onRenameSession, onDeleteSession }) {
  const sessions = useSessionStore((state) => state.filteredSessions());
  const activeSessionId = useSessionStore((state) => state.activeSessionId);

  if (!sessions.length) {
    return <div className="sidebar-empty">No sessions yet</div>;
  }

  return (
    <div className="session-list">
      {sessions.map((session) => (
        <SessionItem
          session={session}
          active={session.id === activeSessionId}
          onClick={onLoadSession}
          onRename={onRenameSession}
          onDelete={onDeleteSession}
          key={session.id}
        />
      ))}
    </div>
  );
}
