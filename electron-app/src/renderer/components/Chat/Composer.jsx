import { ArrowUp, Paperclip, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useChatStore } from "../../stores/chatStore.js";

export function Composer({ onSend }) {
  const [value, setValue] = useState("");
  const textareaRef = useRef(null);
  const isStreaming = useChatStore((state) => state.isStreaming);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) {
      return;
    }
    textarea.style.height = "0px";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  }, [value]);

  function submit() {
    if (isStreaming) {
      return;
    }
    if (onSend(value)) {
      setValue("");
    }
  }

  function onKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className="composer-wrap">
      <button className="icon-button" type="button" title="Attach files" aria-label="Attach files">
        <Paperclip size={18} />
      </button>
      <textarea
        ref={textareaRef}
        value={value}
        rows={1}
        placeholder="Start with a goal"
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <button
        className="send-button"
        type="button"
        title={isStreaming ? "Streaming" : "Send"}
        aria-label={isStreaming ? "Streaming" : "Send"}
        disabled={!value.trim() || isStreaming}
        onClick={submit}
      >
        {isStreaming ? <Square size={15} /> : <ArrowUp size={18} />}
      </button>
    </div>
  );
}
