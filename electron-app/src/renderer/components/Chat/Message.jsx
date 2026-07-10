import { Copy, ChevronRight, FileText } from "lucide-react";
import { useState } from "react";
import { ToolCard } from "../Tools/ToolCard.jsx";
import { MarkdownRenderer } from "../common/MarkdownRenderer.jsx";
import { StreamingIndicator } from "./StreamingIndicator.jsx";

export function Message({ message }) {
  const [copied, setCopied] = useState(false);

  function copyContent() {
    if (!message.content) return;
    navigator.clipboard.writeText(message.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  if (message.role === "user") {
    return (
      <article className="message-row user">
        <div className="user-bubble">
          {message.attachments?.length ? (
            <div className="message-attachments">
              {message.attachments.map((attachment, index) => (
                <div className="message-attachment" key={`${attachment.name}-${index}`}>
                  {attachment.preview && attachment.type.startsWith("image/") ? (
                    <img src={attachment.preview} alt={attachment.name} />
                  ) : (
                    <span className="message-attachment-icon"><FileText size={18} /></span>
                  )}
                  <span>
                    <strong>{attachment.name}</strong>
                    <small>{attachment.size ? `${(attachment.size / 1024).toFixed(1)} KB` : attachment.type}</small>
                  </span>
                </div>
              ))}
            </div>
          ) : null}
          {message.content ? <span>{message.content}</span> : null}
        </div>
      </article>
    );
  }

  if (message.role === "error") {
    return (
      <article className="message-row error">
        <div className="error-bubble">
          <span className="error-label">Error</span>
          <span>{message.content}</span>
        </div>
      </article>
    );
  }

  const isStreaming = message.status === "streaming";

  return (
    <article className={`message-row assistant${isStreaming ? " streaming" : ""}`}>
      {message.status === "streaming" && !message.content && !message.toolCalls?.length ? (
        <div className="thinking-row">
          <ChevronRight size={14} />
          <span>Thinking</span>
          <StreamingIndicator />
        </div>
      ) : null}
      {message.content ? (
        <div className={`assistant-content${isStreaming ? " streaming-content" : ""}`}>
          <MarkdownRenderer content={message.content} />
        </div>
      ) : null}
      {message.toolCalls?.length ? (
        <div className="tool-stack">
          {message.toolCalls.map((call) => (
            <ToolCard call={call} key={call.id || `${call.tool}-${call.at}`} />
          ))}
        </div>
      ) : null}
      {message.status === "streaming" && message.content ? <StreamingIndicator /> : null}
      {message.status === "done" && message.content ? (
        <div className="message-actions">
          <button
            className="action-btn"
            type="button"
            title={copied ? "Copied!" : "Copy"}
            onClick={copyContent}
          >
            <Copy size={14} />
            {copied ? <span>Copied</span> : null}
          </button>
        </div>
      ) : null}
    </article>
  );
}
