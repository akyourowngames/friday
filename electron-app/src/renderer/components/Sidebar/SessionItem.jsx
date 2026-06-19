import { MessageSquare } from "lucide-react";

export function SessionItem({ session, active, onClick }) {
  return (
    <button
      className={`session-item ${active ? "active" : ""}`}
      type="button"
      onClick={() => onClick(session.id)}
      title={session.title}
    >
      <MessageSquare size={14} />
      <span>{session.title || "New session"}</span>
      {session.message_count ? <small>{session.message_count}</small> : null}
    </button>
  );
}
