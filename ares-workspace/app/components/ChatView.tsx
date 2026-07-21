"use client";

import { ArrowDown, ArrowUp, Bot, FileImage, FileText, Mic, Network, Paperclip, Square, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AgentRootRun, Artifact, ChatMessage, PendingFile, TraceEvent } from "@/lib/types";
import { VoiceConversation, type VoiceConversationHandle } from "./VoiceConversation";

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
  cancelMessage: () => void;
  openArtifact: (artifact: Artifact) => void;
  model: string;
  traces: TraceEvent[];
  agentRunsEnabled: boolean;
  agentRuns: AgentRootRun[];
  cancelAgentRun: (runId: string) => void;
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

function runElapsed(run: AgentRootRun, now: number) {
  const start = Date.parse(run.started_at || run.created_at || "");
  if (!Number.isFinite(start)) return "";
  const end = run.completed_at ? Date.parse(run.completed_at) : now || start;
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function MultiAgentRuns({ enabled, runs, cancel, openArtifact }: { enabled: boolean; runs: AgentRootRun[]; cancel: (runId: string) => void; openArtifact: (artifact: Artifact) => void }) {
  const [now, setNow] = useState(0);
  const active = runs.some(run => ["queued", "running"].includes(run.status));
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  if (!enabled || !runs.length) return null;
  return <div className="agent-runs" aria-label="Multi-agent runs">
    {runs.slice(0, 5).map(run => <details className={`agent-run is-${run.status}`} key={run.run_id} open={run.status === "running" || run.status === "queued"}>
      <summary>
        <span className="agent-run-icon"><Network /></span>
        <span><strong>Specialist team</strong><small>{run.prompt_summary || run.activity || "Delegated Ares task"}</small></span>
        <em>{runElapsed(run, now)}</em><i className={`agent-status is-${run.status}`}>{run.status.replace("_", " ")}</i>
      </summary>
      <div className="agent-run-body">
        <div className="agent-run-meta"><span>{run.children?.length || 0} specialist{run.children?.length === 1 ? "" : "s"}</span><span>{run.activity || (run.status === "running" ? "Executing dependency wave" : "Run complete")}</span>{["queued", "running"].includes(run.status) && <button onClick={() => cancel(run.run_id)}><Square size={10} fill="currentColor" /> Cancel</button>}</div>
        <div className="agent-children">{(run.children || []).map(child => <details className={`agent-child is-${child.status}`} key={child.run_id}>
          <summary><span className="agent-avatar"><Bot /></span><span><strong>{child.agent_role}</strong><small>{child.task_id}{child.dependencies?.length ? ` · after ${child.dependencies.join(", ")}` : " · independent"}</small></span><i className={`agent-status is-${child.status}`}>{child.status.replace("_", " ")}</i></summary>
          <div className="agent-child-result">
            <p>{child.activity || child.result_summary || child.error_summary || child.prompt_summary || "Waiting for progress…"}</p>
            {child.current_tool && <span className="agent-tool">Using {child.current_tool}</span>}
            {child.result_content && <MarkdownContent>{child.result_content}</MarkdownContent>}
            {child.artifacts?.length ? <div className="agent-artifacts">{child.artifacts.map(artifact => <button key={artifact.path} onClick={() => openArtifact({ id: artifact.path, path: artifact.path, name: artifact.path.split(/[\\/]/).pop() || "Artifact", mime: artifact.media_type, kind: artifact.media_type === "application/pdf" ? "pdf" : artifact.media_type?.startsWith("image/") ? "image" : artifact.media_type === "text/markdown" ? "markdown" : "file" })}><FileText />{artifact.path.split(/[\\/]/).pop()}</button>)}</div> : null}
          </div>
        </details>)}</div>
        {run.status === "running" && run.activity?.toLowerCase().includes("synth") ? <div className="agent-synthesis"><i /> Root Ares is synthesizing the specialist evidence</div> : null}
      </div>
    </details>)}
  </div>;
}

export function ChatView(props: Props) {
  const { messages, historyLoading, streaming, busy, phase, input, setInput, attachments, removeAttachment, addFiles, sendMessage, cancelMessage, openArtifact, model, traces, agentRunsEnabled, agentRuns, cancelAgentRun } = props;
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const voiceRoomRef = useRef<VoiceConversationHandle>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [voiceOpen, setVoiceOpen] = useState(false);
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
  const submitDraft = () => sendMessage();

  const openVoiceRoom = () => {
    setVoiceOpen(true);
  };

  const activeTrace = [...traces].reverse().find(trace => trace.state === "active");

  return <><section className="view is-active chat-view">
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
        <MultiAgentRuns enabled={agentRunsEnabled} runs={agentRuns} cancel={cancelAgentRun} openArtifact={openArtifact} />
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
          <div className="composer-tools">
            <button className="composer-icon" onClick={() => fileRef.current?.click()} aria-label="Attach files"><Paperclip /></button>
            <button className="composer-icon voice-trigger" onClick={openVoiceRoom} aria-label="Start a voice conversation" title="Talk to Ares"><Mic /></button>
          </div>
          <textarea ref={inputRef} rows={1} value={input} maxLength={50000} placeholder="Message Ares" aria-label="Message Ares" onChange={event => setInput(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submitDraft(); } }} />
          <div className="composer-actions"><button className="mode-select" title={model}><span className="model-dot" /><span>{model || "Model"}</span></button><button className={`send-btn ${busy ? "is-stop" : ""}`} onClick={() => busy ? cancelMessage() : submitDraft()} aria-label={busy ? "Stop Ares response" : "Send message"}>{busy ? <Square size={13} fill="currentColor" /> : <ArrowUp />}</button></div>
        </div>
          <div className="composer-meta"><span aria-live="polite">{busy ? "This chat is running in the background if you switch." : "Use the microphone to talk to Ares live."}</span><span>{attachments.length ? `${attachments.length} file${attachments.length === 1 ? "" : "s"} ready` : "Files stay local"}</span></div>
        <input ref={fileRef} type="file" multiple hidden onChange={event => { if (event.target.files) addFiles(event.target.files); event.target.value = ""; }} />
      </div>
    </div>
  </section><VoiceConversation ref={voiceRoomRef} open={voiceOpen} onClose={() => setVoiceOpen(false)} /></>;
}
