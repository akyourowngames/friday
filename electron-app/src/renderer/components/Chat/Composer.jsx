import {
  ArrowUp,
  FileText,
  Image as ImageIcon,
  Paperclip,
  Square,
  X,
} from "lucide-react";
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { useChatStore } from "../../stores/chatStore.js";

const MAX_FILE_BYTES = 25 * 1024 * 1024;
const MAX_TOTAL_BYTES = 50 * 1024 * 1024;
const MAX_FILES = 10;

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error || new Error(`Could not read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

async function serializeAttachment(item) {
  let path = "";
  if (window.aresDesktop?.getPathForFile) {
    try {
      path = window.aresDesktop.getPathForFile(item.file) || "";
    } catch {
      path = "";
    }
  }
  return {
    name: item.file.name,
    size: item.file.size,
    type: item.file.type || "application/octet-stream",
    path,
    data: path ? undefined : await readAsDataUrl(item.file),
  };
}

export const Composer = forwardRef(function Composer({ onSend }, ref) {
  const [value, setValue] = useState("");
  const [terminalRefs, setTerminalRefs] = useState([]);
  const [attachments, setAttachments] = useState([]);
  const [attachmentError, setAttachmentError] = useState("");
  const [preparing, setPreparing] = useState(false);
  const textareaRef = useRef(null);
  const fileInputRef = useRef(null);
  const attachmentsRef = useRef([]);
  const isStreaming = useChatStore((state) => state.isStreaming);

  useEffect(() => {
    attachmentsRef.current = attachments;
  }, [attachments]);

  useEffect(() => () => {
    for (const item of attachmentsRef.current) {
      if (item.preview) URL.revokeObjectURL(item.preview);
    }
  }, []);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "0px";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
  }, [value]);

  useEffect(() => {
    const handleTerminalRef = (event) => {
      const terminalRef = event.detail;
      if (terminalRef) {
        setTerminalRefs((previous) => [...previous, terminalRef]);
        setValue((previous) => previous ? `${previous} ${terminalRef.label}` : terminalRef.label);
      }
    };
    window.addEventListener("terminal:sendToChat", handleTerminalRef);
    return () => window.removeEventListener("terminal:sendToChat", handleTerminalRef);
  }, []);

  function addFiles(fileList) {
    const incoming = Array.from(fileList || []);
    if (!incoming.length) return;
    const oversized = incoming.find((file) => file.size > MAX_FILE_BYTES);
    if (oversized) {
      setAttachmentError(`${oversized.name} is larger than the 25 MB limit.`);
      return;
    }
    const previous = attachmentsRef.current;
    const seen = new Set(previous.map((item) => item.key));
    const available = Math.max(0, MAX_FILES - previous.length);
    let total = previous.reduce((sum, item) => sum + item.file.size, 0);
    const additions = [];
    for (const file of incoming) {
      const key = `${file.name}:${file.size}:${file.lastModified}`;
      if (seen.has(key)) continue;
      if (additions.length >= available) {
        setAttachmentError(`You can attach up to ${MAX_FILES} files at once.`);
        break;
      }
      if (total + file.size > MAX_TOTAL_BYTES) {
        setAttachmentError("Attachments cannot exceed 50 MB in total.");
        break;
      }
      total += file.size;
      additions.push({
        file,
        key,
        preview: file.type.startsWith("image/") ? URL.createObjectURL(file) : "",
      });
    }
    if (additions.length === incoming.length) setAttachmentError("");
    setAttachments([...previous, ...additions]);
  }

  useImperativeHandle(ref, () => ({ addFiles }), []);

  async function submit() {
    if (isStreaming || preparing || (!value.trim() && !attachments.length)) return;
    setPreparing(true);
    setAttachmentError("");
    try {
      const payload = await Promise.all(attachments.map(serializeAttachment));
      const displayAttachments = attachments.map((item) => ({
        name: item.file.name,
        size: item.file.size,
        type: item.file.type || "application/octet-stream",
        preview: item.preview,
      }));
      if (onSend(value, payload, displayAttachments)) {
        setValue("");
        setTerminalRefs([]);
        setAttachments([]);
      }
    } catch (error) {
      setAttachmentError(error.message || "Could not prepare the attached files.");
    } finally {
      setPreparing(false);
    }
  }

  function onKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  function removeAttachment(key) {
    setAttachments((previous) => previous.filter((item) => {
      if (item.key === key && item.preview) URL.revokeObjectURL(item.preview);
      return item.key !== key;
    }));
  }

  return (
    <div className="composer-container">
      {attachments.length > 0 ? (
        <div className="attachment-tray" aria-label="Attached files">
          {attachments.map((item) => (
            <div className="attachment-chip" key={item.key}>
              {item.preview ? (
                <img src={item.preview} alt="" />
              ) : (
                <span className="attachment-file-icon"><FileText size={16} /></span>
              )}
              <span className="attachment-chip-copy">
                <strong>{item.file.name}</strong>
                <small>{formatBytes(item.file.size)}</small>
              </span>
              <button type="button" title={`Remove ${item.file.name}`} onClick={() => removeAttachment(item.key)}>
                <X size={13} />
              </button>
            </div>
          ))}
        </div>
      ) : null}
      {attachmentError ? <div className="attachment-error">{attachmentError}</div> : null}
      <div className="composer-wrap">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="visually-hidden"
          onChange={(event) => {
            addFiles(event.target.files);
            event.target.value = "";
          }}
        />
        <button
          className="composer-icon-btn"
          type="button"
          title="Attach files"
          aria-label="Attach files"
          onClick={() => fileInputRef.current?.click()}
        >
          <Paperclip size={19} />
        </button>
        {terminalRefs.length > 0 && (
          <div className="terminal-refs">
            {terminalRefs.map((terminalRef, index) => (
              <span key={index} className="terminal-ref-chip">
                {terminalRef.label}
                <button className="terminal-ref-remove" onClick={() => setTerminalRefs((previous) => previous.filter((_, i) => i !== index))}>
                  <X size={10} />
                </button>
              </span>
            ))}
          </div>
        )}
        <textarea
          ref={textareaRef}
          value={value}
          rows={1}
          placeholder={attachments.length ? "Ask Ares about these files…" : "What are we building?"}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <button
          className="composer-send-btn"
          type="button"
          title={isStreaming ? "Streaming" : preparing ? "Preparing files" : "Send"}
          aria-label={isStreaming ? "Streaming" : preparing ? "Preparing files" : "Send"}
          disabled={(!value.trim() && !attachments.length) || isStreaming || preparing}
          onClick={submit}
        >
          {isStreaming || preparing ? <Square size={14} /> : <ArrowUp size={18} />}
        </button>
      </div>
      <div className="composer-file-hint">
        <ImageIcon size={12} /> Images, PDFs, documents, code, archives, audio, and more
      </div>
    </div>
  );
});
