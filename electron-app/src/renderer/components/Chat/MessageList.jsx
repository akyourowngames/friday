import { useEffect, useRef } from "react";
import { useChatStore } from "../../stores/chatStore.js";
import { Message } from "./Message.jsx";

export function MessageList() {
  const messages = useChatStore((state) => state.messages);
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  if (!messages.length) {
    return (
      <div className="empty-state">
        <h1>Ares</h1>
        <p>Start with a goal, ask for a file, search the web, or keep building from memory.</p>
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
