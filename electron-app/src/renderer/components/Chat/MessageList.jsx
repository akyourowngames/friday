import { useEffect, useRef } from "react";
import { Brain, FolderOpen, Search, TerminalSquare } from "lucide-react";
import { useChatStore } from "../../stores/chatStore.js";
import { AresLogo } from "../common/AresLogo.jsx";
import { Message } from "./Message.jsx";

const HINTS = [
  { icon: Search, label: "Search web", prompt: "Search the web for current AI developer news" },
  { icon: FolderOpen, label: "Inspect files", prompt: "Inspect this project and summarize the important files" },
  { icon: Brain, label: "Use memory", prompt: "Search memory for my current preferences" },
  { icon: TerminalSquare, label: "Run code", prompt: "Run a quick Python sanity check" },
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
        <AresLogo size={72} className="god-logo" />
        <h1 className="god-title">ARES AGENT</h1>
        <p className="god-subtitle">
          Ask in your own words. Ares will pick the right tools, use local
          context, and check in before risky steps.
        </p>
        <div className="empty-state-hints">
          {HINTS.map((hint) => {
            const Icon = hint.icon;
            return (
              <button
                className="hint-chip"
                type="button"
                key={hint.label}
                onClick={() => onSend && onSend(hint.prompt)}
              >
                <Icon size={14} />
                {hint.label}
              </button>
            );
          })}
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
