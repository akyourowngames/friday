"use client";

import { ArrowDown, ArrowUp, FileImage, FileText, Paperclip, Square, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Artifact, ChatMessage, PendingFile, TraceEvent } from "@/lib/types";

interface Props {
  messages: ChatMessage[];
  historyLoading: boolean;
  streaming: string;
  busy: boolean;
  phase: "idle" | "thinking" | "streaming";
  input: string;
  setInput: (value: string) => void;
  attachments: PendingFile[];
  removeAttachment: (id: string) => void;
  addFiles: (files: FileList | File[]) => void;
  sendMessage: (prompt?: string) => void;
  openArtifact: (artifact: Artifact) => void;
  model: string;
  traces: TraceEvent[];
}

export function MarkdownContent({ children, streaming = false }: { children: string; streaming?: boolean }) {
  return <div className={`message-copy ${streaming ? "is-streaming" : ""}`}>
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: ({ children: label, ...props }) => <a {...props} target="_blank" rel="noreferrer">{label}</a>,
        input: props => <input {...props} disabled />,
      }}
    >{children}</ReactMarkdown>
    {streaming && <span className="stream-caret" />}
  </div>;
}

function sizeLabel(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function ArtifactCards({ artifacts, open }: { artifacts?: Artifact[]; open: (artifact: Artifact) => void }) {
  if (!artifacts?.length) return null;
  return <div className="artifact-grid">{artifacts.map(artifact => <button className="artifact-card" key={artifact.id || artifact.path} onClick={() => open(artifact)}>
    <span>{artifact.kind === "image" ? <FileImage /> : <FileText />}</span>
    <span><strong>{artifact.name}</strong><small>{artifact.kind === "image" ? "Image" : artifact.kind === "markdown" ? "Markdown" : artifact.kind === "pdf" ? "PDF" : "Generated file"} · Preview</small></span>
    <em>Open</em>
  </button>)}</div>;
}

export function ChatView(props: Props) {
  const { messages, historyLoading, streaming, busy, phase, input, setInput, attachments, removeAttachment, addFiles, sendMessage, openArtifact, model, traces } = props;
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const hasMessages = messages.length > 0 || Boolean(streaming) || busy || historyLoading;

  const scrollToBottom = (behavior: ScrollBehavior = "smooth") => {
    const element = scrollRef.current;
    if (!element) return;
    element.scrollTo({ top: element.scrollHeight, behavior });
    setAtBottom(true);
  };
  useEffect(() => {
    if (!atBottom) return;
    const frame = window.requestAnimationFrame(() => scrollToBottom(streaming ? "auto" : "smooth"));
    return () => window.cancelAnimationFrame(frame);
  // Keep user-controlled scroll position while tokens stream.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, streaming, traces]);
  useEffect(() => {
    if (!inputRef.current) return;
    inputRef.current.style.height = "auto";
    inputRef.current.style.height = `${Math.min(160, inputRef.current.scrollHeight)}px`;
  }, [input]);

  const activeTrace = [...traces].reverse().find(trace => trace.state === "active");

  return <section className="view is-active chat-view">
    <div className="chat-main" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); addFiles(event.dataTransfer.files); }}>
      <div className="messages" ref={scrollRef} aria-live="polite" onScroll={event => { const element = event.currentTarget; setAtBottom(element.scrollHeight - element.scrollTop - element.clientHeight < 80); }}>
        {historyLoading ? <div className="history-skeleton" aria-label="Loading conversation" role="status">
          <div className="history-skeleton-row"><i /><span><b /><b /><b /></span></div>
          <div className="history-skeleton-row assistant"><i /><span><b /><b /><b /></span></div>
          <div className="history-skeleton-row"><i /><span><b /><b /></span></div>
        </div> : messages.map((message, index) => <article className={`message ${message.role}`} key={message.id || `${message.role}-${index}`}>
          <div className="message-avatar">{message.role === "assistant" ? "A" : "YOU"}</div>
          <div>
            <div className="message-head"><strong>{message.role === "assistant" ? "Ares" : "Operator"}</strong><time>{message.created_at ? new Date(message.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "now"}</time></div>
            <MarkdownContent>{message.content}</MarkdownContent>
            {message.tool_calls?.length ? <div className="message-tools">{message.tool_calls.map((tool, i) => <span className="tool-chip" key={i}>{String(tool.name || "tool")}</span>)}</div> : null}
            <ArtifactCards artifacts={message.artifacts} open={openArtifact} />
          </div>
        </article>)}
        {busy && !streaming && !historyLoading ? <article className="message assistant thinking-message"><div className="message-avatar">A</div><div><div className="message-head"><strong>Ares</strong><time>{phase === "thinking" ? "reasoning" : "working"}</time></div><div className="thinking-copy"><span>Thinking through this</span><i /><i /><i /></div></div></article> : null}
        {streaming ? <article className="message assistant"><div className="message-avatar">A</div><div><div className="message-head"><strong>Ares</strong><time>streaming</time></div><MarkdownContent streaming>{streaming}</MarkdownContent></div></article> : null}
      </div>

      {!atBottom && <button className="scroll-to-bottom" onClick={() => scrollToBottom()} aria-label="Scroll to latest message"><ArrowDown /></button>}

      {!hasMessages && <div className="chat-empty">
        <div className="hero-sigil"><span>A</span></div>
        <h1>How can I help?</h1>
        <p className="hero-copy">Ask a question, plan something, or attach a file.</p>
      </div>}

      <div className="composer-dock">
        {activeTrace && <div className="inline-trace"><span className="tool-spinner" /><strong>Using {activeTrace.label}</strong><span>{activeTrace.detail || "Working…"}</span></div>}
        <div className="attachment-strip">{attachments.map(file => <div className="attachment-chip" key={file.id}><span><FileText size={14} /></span><span><strong>{file.name}</strong><small>{sizeLabel(file.size)}</small></span><button onClick={() => removeAttachment(file.id)} aria-label={`Remove ${file.name}`}><X size={13} /></button></div>)}</div>
        <div className="composer">
          <button className="composer-icon" onClick={() => fileRef.current?.click()} aria-label="Attach files"><Paperclip /></button>
          <textarea ref={inputRef} rows={1} value={input} maxLength={50000} placeholder="Message Ares" aria-label="Message Ares" onChange={event => setInput(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(); } }} />
          <div className="composer-actions"><button className="mode-select" title={model}><span className="model-dot" /><span>{model || "Model"}</span></button><button className={`send-btn ${busy ? "is-stop" : ""}`} onClick={() => sendMessage()} disabled={busy} aria-label={busy ? "Ares is responding in this chat" : "Send message"}>{busy ? <Square size={13} fill="currentColor" /> : <ArrowUp />}</button></div>
        </div>
        <div className="composer-meta"><span>{busy ? "This chat is running in the background if you switch." : "Ares can make mistakes. Check important information."}</span><span>{attachments.length ? `${attachments.length} file${attachments.length === 1 ? "" : "s"} ready` : "Files stay local"}</span></div>
        <input ref={fileRef} type="file" multiple hidden onChange={event => { if (event.target.files) addFiles(event.target.files); event.target.value = ""; }} />
      </div>
    </div>
  </section>;
}
