import { ArrowUp, Plus, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useChatStore } from "../../stores/chatStore.js";

export function Composer({ onSend }) {
  const [value, setValue] = useState("");
  const textareaRef = useRef(null);
  const fileInputRef = useRef(null);
  const isStreaming = useChatStore((state) => state.isStreaming);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) {
      return;
    }
    textarea.style.height = "0px";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
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

  function handleFileSelect(event) {
    const files = Array.from(event.target.files || []);
    if (!files.length) return;
    const paths = files.map((f) => f.path || f.name).filter(Boolean);
    if (paths.length) {
      onSend(`Please inspect these file paths:\n${paths.join("\n")}`);
    }
    // Reset so the same file can be picked again
    event.target.value = "";
  }

  return (
    <div className="composer-wrap">
      <input
        ref={fileInputRef}
        type="file"
        multiple
        style={{ display: "none" }}
        onChange={handleFileSelect}
      />
      <button
        className="composer-icon-btn"
        type="button"
        title="Attach files"
        aria-label="Attach files"
        onClick={() => fileInputRef.current?.click()}
      >
        <Plus size={20} />
      </button>
      <textarea
        ref={textareaRef}
        value={value}
        rows={1}
        placeholder="What are we building?"
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <button
        className="composer-send-btn"
        type="button"
        title={isStreaming ? "Streaming" : "Send"}
        aria-label={isStreaming ? "Streaming" : "Send"}
        disabled={!value.trim() || isStreaming}
        onClick={submit}
      >
        {isStreaming ? <Square size={14} /> : <ArrowUp size={18} />}
      </button>
    </div>
  );
}
