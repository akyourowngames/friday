import { useState } from "react";
import { Composer } from "./Composer.jsx";
import { MessageList } from "./MessageList.jsx";

export function ChatArea({ onSend }) {
  const [dragging, setDragging] = useState(false);

  function onDrop(event) {
    event.preventDefault();
    setDragging(false);
    const paths = Array.from(event.dataTransfer.files || [])
      .map((file) => file.path || file.name)
      .filter(Boolean);
    if (paths.length) {
      onSend(`Please inspect these file paths:\n${paths.join("\n")}`);
    }
  }

  return (
    <section
      className={`chat-pane ${dragging ? "dragging" : ""}`}
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
    >
      <MessageList />
      {dragging ? <div className="drop-overlay">Drop files into Ares</div> : null}
      <Composer onSend={onSend} />
    </section>
  );
}
