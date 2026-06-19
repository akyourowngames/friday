import { useEffect, useRef } from "react";
import { useChatStore } from "../../stores/chatStore.js";
import { Message } from "./Message.jsx";

const HINTS = [
  { icon: "🔍", label: "Search the web" },
  { icon: "📁", label: "Read a file" },
  { icon: "🧠", label: "Use memory" },
  { icon: "💻", label: "Run code" },
];

export function MessageList({ onSend }) {
  const messages = useChatStore((state) => state.messages);
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  if (!messages.length) {
    return (
      <div className="empty-state">
        <h1 className="god-title">ARES AGENT</h1>
        <p className="god-subtitle">
          Describe the task in your own words. I'll pick the right tools,
          explain my plan, and check in before risky steps.
        </p>
        <div className="empty-state-hints">
          {HINTS.map((hint) => (
            <button
              className="hint-chip"
              type="button"
              key={hint.label}
              onClick={() => onSend && onSend(hint.label)}
            >
              <span>{hint.icon}</span>
              {hint.label}
            </button>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="message-list">
      {messages.map((message) => (
        <Message message={message} key={message.id} />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
