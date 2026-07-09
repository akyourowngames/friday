import { useMemo } from "react";
import { useSessionStore } from "../../stores/sessionStore.js";
import { SessionItem } from "./SessionItem.jsx";

export function SessionList({ onLoadSession, onRenameSession, onDeleteSession, isStreaming }) {
  const allSessions = useSessionStore((state) => state.sessions);
  const search = useSessionStore((state) => state.search);
  const activeSessionId = useSessionStore((state) => state.activeSessionId);
  const sessions = useMemo(() => {
    const query = search.trim().toLowerCase();
    if (!query) {
      return allSessions;
    }
    return allSessions.filter((session) =>
      `${session.title || ""} ${session.summary || ""}`.toLowerCase().includes(query)
    );
  }, [allSessions, search]);

  if (!sessions.length) {
    return <div className="sidebar-empty">{search.trim() ? "No matching sessions" : "No sessions yet"}</div>;
  }

  return (
    <div className="session-list">
      {sessions.map((session) => (
        <SessionItem
          session={session}
          active={session.id === activeSessionId}
          streaming={session.id === activeSessionId && isStreaming}
          onClick={onLoadSession}
          onRename={onRenameSession}
          onDelete={onDeleteSession}
          key={session.id}
        />
      ))}
    </div>
  );
}
