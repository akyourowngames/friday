import { useRef, useState } from "react";
import { Composer } from "./Composer.jsx";
import { MessageList } from "./MessageList.jsx";

export function ChatArea({ onSend }) {
  const [dragging, setDragging] = useState(false);
  const composerRef = useRef(null);
  const dragDepth = useRef(0);

  function onDrop(event) {
    event.preventDefault();
    setDragging(false);
    dragDepth.current = 0;
    composerRef.current?.addFiles(event.dataTransfer.files);
  }

  return (
    <section
      className={`chat-pane ${dragging ? "dragging" : ""}`}
      onDragEnter={(event) => {
        event.preventDefault();
        dragDepth.current += 1;
        setDragging(true);
      }}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={() => {
        dragDepth.current = Math.max(0, dragDepth.current - 1);
        if (dragDepth.current === 0) setDragging(false);
      }}
      onDrop={onDrop}
    >
      <MessageList onSend={onSend} />
      {dragging ? <div className="drop-overlay">Drop files to attach</div> : null}
      <Composer ref={composerRef} onSend={onSend} />
    </section>
  );
}
