import { AlertTriangle, Bot, User } from "lucide-react";
import { ToolCard } from "../Tools/ToolCard.jsx";
import { MarkdownRenderer } from "../common/MarkdownRenderer.jsx";
import { StreamingIndicator } from "./StreamingIndicator.jsx";

function Avatar({ role }) {
  if (role === "user") {
    return <User size={16} />;
  }
  if (role === "error") {
    return <AlertTriangle size={16} />;
  }
  return <Bot size={16} />;
}

export function Message({ message }) {
  return (
    <article className={`message-row ${message.role}`}>
      <div className="message-avatar">
        <Avatar role={message.role} />
      </div>
      <div className="message-card">
        <div className="message-meta">
          <span>{message.role === "user" ? "You" : message.role === "error" ? "Error" : "Ares"}</span>
        </div>
        {message.content ? <MarkdownRenderer content={message.content} /> : null}
        {message.toolCalls?.length ? (
          <div className="tool-stack">
            {message.toolCalls.map((call) => (
              <ToolCard call={call} key={call.id || `${call.tool}-${call.at}`} />
            ))}
          </div>
        ) : null}
        {message.status === "streaming" ? <StreamingIndicator /> : null}
      </div>
    </article>
  );
}
