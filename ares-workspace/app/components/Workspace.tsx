"use client";

import {
  Menu, Plus, RefreshCw, Search, Settings, Sparkles, X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAresSocket } from "@/lib/useAresSocket";
import type {
  Artifact, ArtifactPreview, AresMessage, ChatMessage, JsonRecord, McpServer, McpState, PendingFile, RuntimeStatus,
  Session, Skill, TraceEvent, ViewId, WatcherMonitor, WatcherState, WorkspaceFile, WorkspaceSettings,
} from "@/lib/types";
import { ChatView, MarkdownContent } from "./ChatView";
import { SettingsHub } from "./SettingsHub";

const emptyWatchers: WatcherState = {
  running: false,
  overview: { monitors: 0, active: 0, paused: 0, failing: 0, unacknowledged_alerts: 0, delivery_failures: 0, total_checks: 0, total_changes: 0, average_latency_ms: 0, checks_24h: 0, success_rate_24h: 100 },
  monitors: [], events: [], checks: [],
};
const emptyMcp: McpState = { summary: { ready: false, configured: 0, connected: 0, tools: 0 }, servers: [] };

type ModalState = { title: string; copy?: string; content: React.ReactNode } | null;
type Toast = { id: string; title: string; copy: string; tone?: "error" | "warn"; sessionId?: number };
type ChatRuntime = {
  messages: ChatMessage[]; streaming: string; busy: boolean; historyLoading: boolean;
  traces: TraceEvent[]; phase: "idle" | "thinking" | "streaming"; hydrated: boolean;
};

function text(value: unknown) { return typeof value === "string" ? value : value === undefined || value === null ? "" : JSON.stringify(value); }
function number(value: unknown) { return Number.isFinite(Number(value)) ? Number(value) : 0; }
function array(value: unknown) { return Array.isArray(value) ? value : []; }
function uid(prefix = "id") { return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`; }

const CHAT_CACHE_KEY = "ares.workspace.chat-cache.v1";
const CHAT_CACHE_LIMIT = 20;
const CHAT_CACHE_MESSAGE_LIMIT = 240;
const newRuntime = (messages: ChatMessage[] = [], hydrated = false): ChatRuntime => ({ messages, streaming: "", busy: false, historyLoading: false, traces: [], phase: "idle", hydrated });
const sessionKey = (id: number) => `session:${id}`;

function normalizeHistory(raw: unknown): ChatMessage[] {
  return array(raw).map(item => {
    const message = item as Record<string, unknown>;
    const calls = array(message.tool_calls).map(tool => {
      const call = tool as Record<string, unknown>;
      return { name: text(call.name || call.tool), content: call.content };
    });
    const artifacts = array(message.artifacts).map(raw => raw as Artifact);
    return {
      id: typeof message.id === "string" || typeof message.id === "number" ? message.id : uid("cached"),
      role: message.role === "user" ? "user" : "assistant",
      content: text(message.content),
      created_at: text(message.created_at),
      tool_calls: calls,
      artifacts,
    } as ChatMessage;
  }).slice(-CHAT_CACHE_MESSAGE_LIMIT);
}

function Modal({ state, close }: { state: ModalState; close: () => void }) {
  if (!state) return null;
  return <div className="modal-layer is-open" aria-hidden="false"><div className="modal-backdrop" onClick={close} /><div className="modal" role="dialog" aria-modal="true"><div className="modal-head"><div><h2>{state.title}</h2>{state.copy && <p>{state.copy}</p>}</div><button onClick={close} aria-label="Close"><X /></button></div>{state.content}</div></div>;
}

function ArtifactViewer({ artifact, close }: { artifact: ArtifactPreview | null; close: () => void }) {
  if (!artifact) return null;
  const isMarkdown = artifact.mime === "text/markdown" || /\.md$/i.test(artifact.name);
  return <div className="artifact-layer" role="dialog" aria-modal="true" aria-label={`Preview ${artifact.name}`}>
    <button className="artifact-backdrop" onClick={close} aria-label="Close artifact preview" />
    <section className="artifact-viewer">
      <header><div><small>ARES ARTIFACT</small><strong>{artifact.name}</strong></div><button onClick={close} aria-label="Close"><X /></button></header>
      <div className="artifact-canvas">
        {!artifact.content && !artifact.data_url ? <div className="artifact-loading"><i /><span>Loading local preview…</span></div> : null}
        {/* Data URLs come from the local Ares runtime and cannot use Next image optimization. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        {artifact.data_url && artifact.mime?.startsWith("image/") ? <img src={artifact.data_url} alt={artifact.name} /> : null}
        {artifact.data_url && artifact.mime === "application/pdf" ? <iframe src={artifact.data_url} title={artifact.name} /> : null}
        {artifact.content && isMarkdown ? <div className="artifact-markdown"><MarkdownContent>{artifact.content}</MarkdownContent></div> : null}
        {artifact.content && !isMarkdown ? <pre><code>{artifact.content}</code></pre> : null}
        {artifact.data_url && !artifact.mime?.startsWith("image/") && artifact.mime !== "application/pdf" ? <a className="artifact-open-link" href={artifact.data_url} download={artifact.name}>Save {artifact.name}</a> : null}
      </div>
    </section>
  </div>;
}

function FormField({ label, children, wide = false, hint }: { label: string; children: React.ReactNode; wide?: boolean; hint?: string }) {
  return <div className={`field ${wide ? "span-2" : ""}`}><label>{label}</label>{children}{hint && <p className="field-hint">{hint}</p>}</div>;
}

export function Workspace() {
  const [view, setView] = useState<ViewId>("chat");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [activeChatKey, setActiveChatKey] = useState("draft-initial");
  const [chatStates, setChatStates] = useState<Record<string, ChatRuntime>>({ "draft-initial": newRuntime([], true) });
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<PendingFile[]>([]);
  const [status, setStatus] = useState<RuntimeStatus>({});
  const [watchers, setWatchers] = useState<WatcherState>(emptyWatchers);
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [selectedSkill, setSelectedSkill] = useState<Skill>();
  const [mcp, setMcp] = useState<McpState>(emptyMcp);
  const [settingsData, setSettingsData] = useState<WorkspaceSettings>({});
  const [savingSettings, setSavingSettings] = useState(false);
  const [modal, setModal] = useState<ModalState>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [artifactPreview, setArtifactPreview] = useState<ArtifactPreview | null>(null);
  const [threadQuery, setThreadQuery] = useState("");
  const sendRef = useRef<(payload: JsonRecord & { type: string }) => boolean>(() => false);
  const activeToolIds = useRef<Record<string, string>>({});
  const sessionIdRef = useRef<number | null>(null);
  const activeChatKeyRef = useRef(activeChatKey);
  const chatStatesRef = useRef(chatStates);
  const sessionsRef = useRef<Session[]>([]);
  const requestChatRef = useRef<Map<string, string>>(new Map());
  const prefetchRequestedRef = useRef<Set<number>>(new Set());
  const sessionCacheRef = useRef<Map<number, ChatMessage[]>>(new Map());
  const threadQueryRef = useRef("");
  const streamQueueRef = useRef<Map<string, string>>(new Map());
  const streamFrameRef = useRef<Map<string, number>>(new Map());

  const updateChat = useCallback((key: string, updater: (current: ChatRuntime) => ChatRuntime) => {
    setChatStates(current => {
      const next = { ...current, [key]: updater(current[key] || newRuntime()) };
      chatStatesRef.current = next;
      return next;
    });
  }, []);

  const enqueueStream = useCallback((key: string, chunk: string) => {
    streamQueueRef.current.set(key, (streamQueueRef.current.get(key) || "") + chunk);
    if (streamFrameRef.current.has(key)) return;
    const pump = () => {
      const queued = streamQueueRef.current.get(key) || "";
      if (!queued) { streamFrameRef.current.delete(key); return; }
      const size = Math.min(14, Math.max(1, Math.ceil(queued.length / 10)));
      updateChat(key, current => ({ ...current, streaming: current.streaming + queued.slice(0, size), phase: "streaming" }));
      streamQueueRef.current.set(key, queued.slice(size));
      streamFrameRef.current.set(key, window.requestAnimationFrame(pump));
    };
    streamFrameRef.current.set(key, window.requestAnimationFrame(pump));
  }, [updateChat]);

  const stopStream = useCallback((key: string) => {
    const frame = streamFrameRef.current.get(key);
    if (frame !== undefined) window.cancelAnimationFrame(frame);
    streamFrameRef.current.delete(key);
    streamQueueRef.current.delete(key);
  }, []);

  const cacheSession = useCallback((id: number, next: ChatMessage[]) => {
    const cache = sessionCacheRef.current;
    cache.set(id, next.slice(-CHAT_CACHE_MESSAGE_LIMIT));
    try {
      const persisted = [...cache.entries()].slice(-CHAT_CACHE_LIMIT);
      window.sessionStorage.setItem(CHAT_CACHE_KEY, JSON.stringify(Object.fromEntries(persisted)));
    } catch { /* Private browsing and quota limits should never block chat. */ }
  }, []);

  useEffect(() => {
    try {
      const stored = JSON.parse(window.sessionStorage.getItem(CHAT_CACHE_KEY) || "{}") as Record<string, unknown>;
      for (const [key, value] of Object.entries(stored)) {
        const id = Number(key);
        if (Number.isInteger(id) && id > 0) {
          const messages = normalizeHistory(value);
          sessionCacheRef.current.set(id, messages);
          chatStatesRef.current = { ...chatStatesRef.current, [sessionKey(id)]: newRuntime(messages, true) };
        }
      }
      setChatStates(chatStatesRef.current);
    } catch { /* A malformed cache is simply treated as empty. */ }
  }, []);

  const toast = useCallback((title: string, copy: string, tone?: Toast["tone"], targetSessionId?: number) => {
    const id = uid("toast");
    setToasts(current => [...current.slice(-3), { id, title, copy, tone, sessionId: targetSessionId }]);
    window.setTimeout(() => setToasts(current => current.filter(item => item.id !== id)), 4600);
  }, []);

  const requestInitialState = useCallback(() => {
    for (const type of ["list_sessions", "get_status", "get_workspace_settings", "list_skills", "get_mcp_state", "get_watcher_state", "list_workspace_files"]) sendRef.current({ type });
  }, []);

  const handleMessage = useCallback((message: AresMessage) => {
    const requestId = text(message.request_id);
    const eventSessionId = number(message.session_id);
    const eventKey = eventSessionId ? sessionKey(eventSessionId) : requestChatRef.current.get(requestId) || activeChatKeyRef.current;
    switch (message.type) {
      case "socket_open": requestInitialState(); break;
      case "session_info": {
        if (message.model) setStatus(current => ({ ...current, model: text(message.model) }));
        break;
      }
      case "status": setStatus(message as unknown as RuntimeStatus); break;
      case "sessions": {
        const query = text(message.query);
        if (query !== threadQueryRef.current) break;
        const nextSessions = array(message.sessions).map(raw => raw as Session);
        setSessions(nextSessions); sessionsRef.current = nextSessions;
        if (!query) {
          const missing = nextSessions.map(item => item.id).filter(id => !chatStatesRef.current[sessionKey(id)]?.hydrated && !prefetchRequestedRef.current.has(id));
          if (missing.length) {
            missing.forEach(id => prefetchRequestedRef.current.add(id));
            for (let index = 0; index < missing.length; index += 400) {
              sendRef.current({ type: "prefetch_sessions", session_ids: missing.slice(index, index + 400) });
            }
          }
        }
        break;
      }
      case "chat_started": {
        if (!eventSessionId) break;
        const targetKey = sessionKey(eventSessionId);
        const sourceKey = requestChatRef.current.get(requestId) || eventKey;
        requestChatRef.current.set(requestId, targetKey);
        if (sourceKey !== targetKey) {
          setChatStates(current => {
            const source = current[sourceKey] || newRuntime();
            const next = { ...current, [targetKey]: source };
            delete next[sourceKey];
            chatStatesRef.current = next;
            return next;
          });
          if (activeChatKeyRef.current === sourceKey) {
            activeChatKeyRef.current = targetKey; setActiveChatKey(targetKey);
            sessionIdRef.current = eventSessionId; setSessionId(eventSessionId);
          }
        }
        break;
      }
      case "session_history": {
        const loadedSessionId = number(message.session_id);
        const loadedMessages = normalizeHistory(message.messages);
        cacheSession(loadedSessionId, loadedMessages);
        updateChat(sessionKey(loadedSessionId), current => current.busy ? current : { ...current, messages: loadedMessages, streaming: "", historyLoading: false, hydrated: true });
        break;
      }
      case "session_histories": {
        for (const raw of array(message.histories)) {
          const history = raw as Record<string, unknown>; const id = number(history.session_id);
          if (!id) continue;
          const loadedMessages = normalizeHistory(history.messages);
          cacheSession(id, loadedMessages);
          updateChat(sessionKey(id), current => current.busy ? current : { ...current, messages: loadedMessages, historyLoading: false, hydrated: true });
        }
        break;
      }
      case "response_status": {
        const stage = text(message.stage) || "thinking";
        updateChat(eventKey, current => ({ ...current, phase: stage === "complete" ? "idle" : stage === "streaming" ? "streaming" : "thinking" }));
        break;
      }
      case "content": enqueueStream(eventKey, text(message.text)); break;
      case "response_done": {
        stopStream(eventKey);
        const content = text(message.content);
        const calls = array(message.tool_calls).map(raw => { const call = raw as Record<string, unknown>; return { name: text(call.tool || call.name), content: call.content }; });
        const artifacts = array(message.artifacts).map(raw => raw as Artifact);
        updateChat(eventKey, current => {
          const nextMessages = content || calls.length || artifacts.length ? [...current.messages, { id: uid("assistant"), role: "assistant" as const, content, created_at: new Date().toISOString(), tool_calls: calls, artifacts }] : current.messages;
          if (eventSessionId) cacheSession(eventSessionId, nextMessages);
          return { ...current, messages: nextMessages, streaming: "", busy: false, phase: "idle", hydrated: true };
        });
        if (eventSessionId && activeChatKeyRef.current !== eventKey) {
          const title = sessionsRef.current.find(item => item.id === eventSessionId)?.title || "Background chat";
          toast("Response ready", `${title} finished while you were elsewhere.`, undefined, eventSessionId);
        }
        if (requestId) requestChatRef.current.delete(requestId);
        break;
      }
      case "tool_start": {
        const tool = text(message.tool) || "Tool"; const traceId = uid("trace"); activeToolIds.current[`${eventKey}:${tool}`] = traceId;
        updateChat(eventKey, current => ({ ...current, traces: [...current.traces, { id: traceId, label: tool, detail: "Executing", state: "active", at: new Date() }] }));
        break;
      }
      case "tool_args": {
        const tool = text(message.tool); const active = activeToolIds.current[`${eventKey}:${tool}`]; const detail = text(message.args).slice(0, 180) || "Input prepared";
        updateChat(eventKey, current => ({ ...current, traces: current.traces.map(item => item.id === active ? { ...item, detail } : item) }));
        break;
      }
      case "tool_result": {
        const tool = text(message.tool); const mapKey = `${eventKey}:${tool}`; const active = activeToolIds.current[mapKey]; delete activeToolIds.current[mapKey];
        updateChat(eventKey, current => ({ ...current, traces: current.traces.map(item => item.id === active ? { ...item, detail: "Completed", state: "done" } : item) }));
        break;
      }
      case "artifact_content": setArtifactPreview({ id: text(message.path), path: text(message.path), name: text(message.name), mime: text(message.mime), content: text(message.content) || undefined, data_url: text(message.data_url) || undefined }); break;
      case "workspace_files": setFiles(array(message.files).map(item => item as WorkspaceFile)); break;
      case "workspace_file_uploaded": setUploading(false); toast("File secured", `${text((message.file as JsonRecord | undefined)?.name) || "File"} is ready across conversations.`); break;
      case "workspace_files_error": setUploading(false); toast("Upload rejected", text(message.message), "error"); break;
      case "skills": setSkills(array(message.skills).map(item => item as Skill)); setCategories(array(message.categories).map(String)); break;
      case "skill_detail": setSelectedSkill(message.skill as unknown as Skill); break;
      case "skill_saved": setSelectedSkill(message.skill as unknown as Skill); setModal(null); toast("Skill saved", "Ares refreshed the active skill catalog."); break;
      case "skill_deleted": setSelectedSkill(undefined); toast("Skill removed", `${text(message.name)} was removed.`); break;
      case "skill_draft": openSkillEditor({ name: text(message.name), category: text(message.category), source: text(message.source), description: "AI-drafted skill", version: "1.0", path: "", editable: true, model_invocable: true, files: [] }, true); break;
      case "skills_error": toast("Skill operation failed", text(message.message), "error"); break;
      case "workspace_settings": setSettingsData((message.settings || {}) as WorkspaceSettings); break;
      case "workspace_settings_saved": setSettingsData((message.settings || {}) as WorkspaceSettings); setSavingSettings(false); {
        const restart = array(message.restart_required).map(String); toast("Settings saved", restart.length ? `Restart needed for: ${restart.join(", ")}.` : "All changes are live.", restart.length ? "warn" : undefined); break;
      }
      case "mcp_state": setMcp(message as unknown as McpState); break;
      case "mcp_server_saved": setModal(null); toast("MCP server saved", `${text(message.name)} is reconnecting.`); break;
      case "mcp_server_deleted": toast("MCP server removed", `${text(message.name)} no longer exposes tools.`); break;
      case "mcp_reconnected": toast("Reconnect complete", "MCP readiness and tool schemas were refreshed."); break;
      case "watcher_state": setWatchers(message as unknown as WatcherState); break;
      case "watcher_action_result": setModal(null); toast("Watcher operation complete", `${text(message.action)} applied to the shared fleet.`); break;
      case "watcher_error": toast("Watcher operation failed", text(message.message), "error"); break;
      case "model_updated": setStatus(current => ({ ...current, model: text(message.model) })); break;
      case "error": updateChat(eventKey, current => ({ ...current, busy: false, streaming: "", phase: "idle", traces: current.traces.map(item => item.state === "active" ? { ...item, state: "error" as const } : item) })); setSavingSettings(false); toast("Ares runtime error", text(message.message), "error"); break;
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheSession, enqueueStream, requestInitialState, stopStream, toast, updateChat]);

  const { connection, runtime, send, reconnect } = useAresSocket(handleMessage);
  useEffect(() => { sendRef.current = send; }, [send]);
  useEffect(() => { sessionIdRef.current = sessionId; }, [sessionId]);
  useEffect(() => { activeChatKeyRef.current = activeChatKey; }, [activeChatKey]);
  useEffect(() => { chatStatesRef.current = chatStates; }, [chatStates]);
  useEffect(() => { sessionsRef.current = sessions; }, [sessions]);
  useEffect(() => { threadQueryRef.current = threadQuery; }, [threadQuery]);
  useEffect(() => {
    if (connection !== "online") return;
    const timer = window.setTimeout(() => send({ type: "list_sessions", query: threadQuery.trim() }), 180);
    return () => window.clearTimeout(timer);
  }, [connection, send, threadQuery]);
  useEffect(() => () => {
    streamFrameRef.current.forEach(frame => window.cancelAnimationFrame(frame));
  }, []);

  const navigate = useCallback((next: ViewId) => { setView(next); setSidebarOpen(false); if (next === "settings") send({ type: "get_workspace_settings" }); }, [send]);

  useEffect(() => {
    const keyboard = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "n") { event.preventDefault(); newChat(); }
      if (event.key === "Escape") setModal(null);
    };
    window.addEventListener("keydown", keyboard); return () => window.removeEventListener("keydown", keyboard);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const newChat = useCallback(() => {
    const key = uid("draft");
    updateChat(key, () => newRuntime([], true)); activeChatKeyRef.current = key; setActiveChatKey(key);
    send({ type: "new_session" }); sessionIdRef.current = null; setSessionId(null); setThreadQuery(""); navigate("chat");
  }, [send, navigate, updateChat]);
  const loadSession = (id: number) => {
    const key = sessionKey(id);
    sessionIdRef.current = id;
    setSessionId(id);
    activeChatKeyRef.current = key; setActiveChatKey(key);
    const cached = sessionCacheRef.current.get(id);
    updateChat(key, current => current.hydrated || current.busy ? current : { ...current, messages: cached || [], historyLoading: !cached });
    send({ type: "load_session", session_id: id });
    navigate("chat");
  };
  const sessionMenu = (session: Session) => {
    const action = window.prompt(`Type "rename" or "delete" for “${session.title}”.`, "rename");
    if (action === "rename") { const title = window.prompt("New thread title", session.title); if (title?.trim()) send({ type: "rename_session", session_id: session.id, title: title.trim() }); }
    if (action === "delete" && window.confirm(`Delete “${session.title}” permanently?`)) send({ type: "delete_session", session_id: session.id });
  };

  const addChatFiles = async (incoming: FileList | File[]) => {
    const values = Array.from(incoming);
    for (const file of values) {
      if (file.size > 25 * 1024 * 1024) { toast("File too large", `${file.name} exceeds the 25 MB limit.`, "error"); continue; }
      const data = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(reader.error); reader.readAsDataURL(file); });
      setAttachments(current => [...current, { id: uid("attachment"), name: file.name, type: file.type || "application/octet-stream", size: file.size, data }]);
    }
  };
  const sendMessage = (prompt?: string) => {
    const key = activeChatKeyRef.current;
    const currentChat = chatStatesRef.current[key] || newRuntime();
    if (currentChat.busy) return;
    const content = (prompt ?? input).trim();
    if (!content && !attachments.length) return;
    if (connection !== "online") { toast("Runtime offline", "Reconnect Ares before sending a message.", "error"); return; }
    const payload = attachments.map(item => item.path ? { name: item.name, type: item.type, path: item.path } : { name: item.name, type: item.type, data: item.data });
    const requestId = uid("request"); requestChatRef.current.set(requestId, key);
    send({ type: "chat", request_id: requestId, content, session_id: sessionIdRef.current, attachments: payload });
    updateChat(key, current => {
      const next = [...current.messages, { id: uid("user"), role: "user" as const, content: content || `Attached: ${attachments.map(item => item.name).join(", ")}`, created_at: new Date().toISOString() }];
      if (sessionIdRef.current) cacheSession(sessionIdRef.current, next);
      return { ...current, messages: next, busy: true, historyLoading: false, streaming: "", phase: "thinking", traces: [] };
    });
    setInput(""); setAttachments([]); stopStream(key);
  };

  const uploadLibrary = async (incoming: FileList | File[]) => {
    setUploading(true);
    for (const file of Array.from(incoming)) {
      if (file.size > 25 * 1024 * 1024) { toast("File too large", `${file.name} exceeds 25 MB.`, "error"); continue; }
      try {
        const data = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(reader.error); reader.readAsDataURL(file); });
        send({ type: "upload_workspace_file", file: { name: file.name, type: file.type || "application/octet-stream", data } });
      } catch { toast("Read failed", `Could not read ${file.name}.`, "error"); }
    }
  };
  const attachLibrary = (file: WorkspaceFile) => { setAttachments(current => current.some(item => item.libraryId === file.id) ? current : [...current, { id: uid("library"), libraryId: file.id, name: file.name, type: file.type, size: file.size, path: file.path }]); navigate("chat"); toast("Attached to composer", `${file.name} is ready for the next message.`); };

  function openWatcherEditor(monitor?: WatcherMonitor) {
    const formId = "watcher-editor-form";
    const config = JSON.stringify(monitor?.config || (monitor?.type === "browser" ? { preset: "instagram_dm", navigate: true, change_detection: "diff" } : {}), null, 2);
    const selectedGoalIds = (monitor?.linked_goals || []).map(goal => String(goal.goal_id));
    setModal({
      title: monitor ? "Edit watcher" : "Deploy watcher",
      copy: "Route observations into durable goals while keeping progress changes explicit and reviewable.",
      content: <form id={formId} onSubmit={event => {
        event.preventDefault();
        const data = new FormData(event.currentTarget);
        let parsed: JsonRecord = {};
        try { parsed = JSON.parse(String(data.get("config") || "{}")) as JsonRecord; }
        catch { toast("Invalid watcher config", "Config must be valid JSON.", "error"); return; }
        const args: JsonRecord = {
          name: String(data.get("name") || ""),
          type: String(data.get("type") || "website"),
          preset: String(data.get("preset") || "") || undefined,
          url: String(data.get("url") || "") || undefined,
          interval_seconds: Number(data.get("interval_seconds") || 900),
          ai_action: String(data.get("ai_action") || "notify"),
          ai_prompt: String(data.get("ai_prompt") || "") || undefined,
          goal_ids: data.getAll("goal_ids").map(value => Number(value)),
          config: parsed,
          enabled: true,
        };
        if (monitor) args.watcher_id = monitor.id;
        send({ type: "watcher_action", action: monitor ? "update" : "create", arguments: args });
      }}>
        <div className="modal-body"><div className="field-grid">
          <FormField label="Name"><input name="name" defaultValue={monitor?.name || ""} required autoFocus /></FormField>
          <FormField label="Type"><select name="type" defaultValue={monitor?.type || "website"}><option value="website">Public website</option><option value="browser">Authenticated browser / DMs</option><option value="custom">REST / JSON endpoint</option><option value="tool">Ares or MCP tool workflow</option><option value="instagram">Instagram Graph API</option></select></FormField>
          <FormField label="Quick preset"><select name="preset" defaultValue={String(monitor?.config?.preset || "")}><option value="">Custom configuration</option><option value="instagram_dm">Authenticated Instagram DMs</option><option value="browser_page">Authenticated browser page</option></select></FormField>
          <FormField label="Cadence (seconds)"><input name="interval_seconds" type="number" min={20} defaultValue={monitor?.interval_seconds || 900} /></FormField>
          <FormField label="Target URL" wide hint="Instagram DM preset fills the inbox URL automatically when this is blank."><input name="url" defaultValue={monitor?.url || ""} placeholder="https://…" /></FormField>
          <FormField label="On change"><select name="ai_action" defaultValue={monitor?.ai_action || "notify"}><option value="notify">Notify</option><option value="suggest">Analyze and suggest</option><option value="auto">Auto action</option></select></FormField>
          <FormField label="AI analysis prompt"><textarea name="ai_prompt" defaultValue={monitor?.ai_prompt || ""} placeholder="How Ares should judge and summarize a change." /></FormField>
          <FormField label="Linked goals" wide hint="Select multiple outcomes. Signals become goal evidence; watcher checks never mutate goal progress automatically.">
            <select className="goal-picker" name="goal_ids" multiple size={Math.min(7, Math.max(3, watchers.goals?.length || 3))} defaultValue={selectedGoalIds}>
              {(watchers.goals || []).map(goal => <option key={goal.goal_id} value={goal.goal_id}>#{goal.goal_id} · {goal.title} · {goal.progress_percent}% · {goal.status}</option>)}
            </select>
            {!watchers.goals?.length && <p className="field-hint">No goals yet. Ask Ares to create an outcome, then refresh watcher state.</p>}
          </FormField>
          <FormField label="Fetcher / workflow config (JSON)" wide><textarea className="code-field" name="config" defaultValue={config} spellCheck={false} /></FormField>
        </div></div>
        <div className="modal-foot"><button type="button" className="secondary-btn" onClick={() => setModal(null)}>Cancel</button><button type="submit" className="primary-btn">{monitor ? "Save watcher" : "Deploy watcher"}</button></div>
      </form>,
    });
  }

  function openSkillEditor(skill?: Skill, createMode = false) {
    const source = skill?.source || `---\nname: ${skill?.name || "new-skill"}\ndescription: Describe when Ares should use this skill.\ncategory: ${skill?.category || "general"}\nversion: 1.0.0\n---\n\n# Workflow\n\n1. Inspect the request and required context.\n2. Execute the workflow safely.\n3. Verify the result before reporting completion.\n`;
    setModal({ title: createMode || !skill ? "Create skill" : "Edit skill", copy: "Ares validates metadata, source, and ownership before activating the skill.", content: <form onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget); const name = String(data.get("name") || ""); const category = String(data.get("category") || "general"); const skillSource = String(data.get("source") || ""); send({ type: createMode || !skill ? "create_skill" : "update_skill", name, category, source: skillSource }); }}><div className="modal-body"><div className="field-grid"><FormField label="Skill name"><input name="name" defaultValue={skill?.name || ""} required readOnly={Boolean(skill && !createMode)} /></FormField><FormField label="Category"><input name="category" defaultValue={skill?.category || "general"} /></FormField><FormField label="SKILL.md" wide><textarea className="code-field" name="source" defaultValue={source} spellCheck={false} required /></FormField></div></div><div className="modal-foot"><button type="button" className="secondary-btn" onClick={() => setModal(null)}>Cancel</button><button className="primary-btn" type="submit">Save & activate</button></div></form> });
  }

  const openSkillDraft = () => setModal({ title: "Draft with Ares", copy: "Describe the capability; Ares will create a complete, editable SKILL.md contract.", content: <form onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget); send({ type: "draft_skill", name: String(data.get("name") || "new-skill"), category: String(data.get("category") || "general"), goal: String(data.get("goal") || "") }); setModal(null); toast("Drafting skill", "Ares is designing the workflow and safety contract."); }}><div className="modal-body"><div className="field-grid"><FormField label="Name"><input name="name" placeholder="incident-triage" required /></FormField><FormField label="Category"><input name="category" defaultValue="general" /></FormField><FormField label="What should this skill do?" wide><textarea name="goal" required autoFocus placeholder="Trigger conditions, inputs, workflow, safety limits, and expected output…" /></FormField></div></div><div className="modal-foot"><button type="button" className="secondary-btn" onClick={() => setModal(null)}>Cancel</button><button className="primary-btn" type="submit"><Sparkles />Draft skill</button></div></form> });

  function openMcpEditor(server?: McpServer) {
    const env = JSON.stringify(server?.env || {}, null, 2);
    setModal({ title: server ? "Edit MCP server" : "Add MCP server", copy: "Connect stdio, Streamable HTTP, or SSE. Secrets remain write-only.", content: <form onSubmit={event => { event.preventDefault(); const data = new FormData(event.currentTarget); let parsedEnv: Record<string, string> = {}; try { parsedEnv = JSON.parse(String(data.get("env") || "{}")) as Record<string, string>; } catch { toast("Invalid environment", "Environment must be a JSON object.", "error"); return; } send({ type: "save_mcp_server", original_name: server?.name, server: { name: String(data.get("name") || ""), transport: String(data.get("transport") || "stdio"), server_url: String(data.get("server_url") || ""), command: String(data.get("command") || ""), args: String(data.get("args") || "").split(/\r?\n/).map(item => item.trim()).filter(Boolean), env: parsedEnv, oauth_client_id: String(data.get("oauth_client_id") || ""), oauth_client_secret: String(data.get("oauth_client_secret") || ""), oauth_scopes: String(data.get("oauth_scopes") || "").split(/[ ,]+/).filter(Boolean), timeout_seconds: Number(data.get("timeout_seconds") || 60) } }); }}><div className="modal-body"><div className="field-grid"><FormField label="Server name"><input name="name" defaultValue={server?.name || ""} required /></FormField><FormField label="Transport"><select name="transport" defaultValue={server?.transport || "stdio"}><option value="stdio">stdio process</option><option value="streamable_http">Streamable HTTP</option><option value="sse">Legacy SSE</option></select></FormField><FormField label="Remote URL" wide><input name="server_url" defaultValue={server?.server_url || server?.endpoint || ""} placeholder="https://mcp.example.com/mcp" /></FormField><FormField label="Command"><input name="command" defaultValue={server?.command || ""} placeholder="npx / uvx / executable" /></FormField><FormField label="Timeout seconds"><input name="timeout_seconds" type="number" min={1} max={600} defaultValue={server?.timeout_seconds || 60} /></FormField><FormField label="Arguments (one per line)" wide><textarea name="args" defaultValue={(server?.args || []).join("\n")} /></FormField><FormField label="Environment JSON" wide hint="Blank values preserve existing secret values by key."><textarea className="code-field" name="env" defaultValue={env} spellCheck={false} /></FormField><FormField label="OAuth client ID"><input name="oauth_client_id" defaultValue={server?.oauth_client_id || ""} /></FormField><FormField label="OAuth secret"><input name="oauth_client_secret" type="password" placeholder={server?.oauth_client_secret_configured ? "Configured — blank preserves" : "Optional"} autoComplete="off" /></FormField><FormField label="OAuth scopes" wide><input name="oauth_scopes" defaultValue={(server?.oauth_scopes || []).join(" ")} placeholder="read write" /></FormField></div></div><div className="modal-foot"><button type="button" className="secondary-btn" onClick={() => setModal(null)}>Cancel</button><button type="submit" className="primary-btn">Save & connect</button></div></form> });
  }

  const userName = String(settingsData.identity?.user_name || "Operator");
  const activeSession = sessions.find(session => session.id === sessionId);
  const visibleSessions = sessions;
  const activeChat = chatStates[activeChatKey] || newRuntime();

  return <>
    <div className={`app-shell ${sidebarOpen ? "sidebar-open" : ""} ${view === "settings" ? "is-settings" : ""}`}>
      <aside className="sidebar" aria-label="Primary navigation">
        <div className="brand-row"><button className="brand" onClick={() => navigate("chat")}><span className="brand-mark">A</span><span><strong>ARES</strong><small>Personal workspace</small></span></button><button className="icon-btn mobile-only" onClick={() => setSidebarOpen(false)} aria-label="Close navigation"><X /></button></div>
        <button className="new-chat" onClick={newChat}><Plus /><span>New chat</span><kbd>⌘ N</kbd></button>
        <div className="thread-search"><Search /><input value={threadQuery} onChange={event => setThreadQuery(event.target.value)} placeholder="Search every conversation" aria-label="Search chats" />{threadQuery && <button onClick={() => setThreadQuery("")} aria-label="Clear chat search"><X /></button>}</div>
        <div className="sidebar-section sessions-section"><div className="section-label"><span>Chats</span><button className="tiny-btn" onClick={() => send({ type: "list_sessions" })} aria-label="Refresh chats"><RefreshCw /></button></div><div className="session-list">{visibleSessions.map(session => { const backgroundBusy = chatStates[sessionKey(session.id)]?.busy; return <button className={`session-item ${session.id === sessionId ? "is-active" : ""} ${backgroundBusy ? "is-running" : ""}`} key={session.id} onClick={() => loadSession(session.id)}><span><strong>{session.title || "New chat"}</strong><small>{backgroundBusy ? "Ares is responding…" : `${session.message_count} messages`}</small></span>{backgroundBusy && <i className="session-running-dot" />}<span className="session-menu" role="button" tabIndex={0} onClick={event => { event.stopPropagation(); sessionMenu(session); }}>•••</span></button>; })}{!sessions.length && <div className="sidebar-skeleton" />}{sessions.length > 0 && !visibleSessions.length && <p className="no-chats">No chats found</p>}</div></div>
        <div className="sidebar-footer"><button className={`nav-item ${view === "settings" ? "is-active" : ""}`} onClick={() => navigate("settings")}><Settings /><span>Settings</span></button><div className="runtime-card"><span className={`connection-dot is-${connection === "online" ? "online" : connection === "offline" ? "offline" : ""}`} /><span><strong>{connection === "online" ? "Ares is ready" : connection === "offline" ? "Ares is offline" : "Connecting"}</strong><small>{connection === "online" ? "Local runtime" : runtime.websocket_url || "ws://localhost:8765"}</small></span><button className="tiny-btn" onClick={() => void reconnect()} aria-label="Reconnect"><RefreshCw /></button></div></div>
      </aside>
      <div className="sidebar-scrim" onClick={() => setSidebarOpen(false)} />
      <main className="main-stage">
        {view === "chat" && <><header className="topbar"><div className="topbar-left"><button className="icon-btn mobile-only" onClick={() => setSidebarOpen(true)} aria-label="Open navigation"><Menu /></button><strong className="chat-title">{activeSession?.title || "New chat"}</strong></div><div className="topbar-actions"><span className="simple-status"><span className={`connection-dot ${connection === "online" ? "is-online" : "is-offline"}`} />{connection === "online" ? "Ready" : connection}</span><button className="avatar-btn" onClick={() => navigate("settings")} aria-label="Open settings">{userName.split(/\s+/).map(part => part[0]).join("").slice(0, 2).toUpperCase() || "OP"}</button></div></header><ChatView messages={activeChat.messages} historyLoading={activeChat.historyLoading} streaming={activeChat.streaming} busy={activeChat.busy} phase={activeChat.phase} input={input} setInput={setInput} attachments={attachments} removeAttachment={id => setAttachments(current => current.filter(item => item.id !== id))} addFiles={addChatFiles} sendMessage={sendMessage} openArtifact={artifact => { setArtifactPreview({ ...artifact }); send({ type: "get_artifact", path: artifact.path }); }} model={status.model || "Ares model"} traces={activeChat.traces} /></>}
        {view === "settings" && <SettingsHub settings={settingsData} savingSettings={savingSettings} saveSettings={next => { setSavingSettings(true); send({ type: "save_workspace_settings", settings: next as unknown as JsonRecord }); }} watchers={watchers} refreshWatchers={() => send({ type: "get_watcher_state" })} createWatcher={() => openWatcherEditor()} editWatcher={openWatcherEditor} watcherAction={(action, arguments_) => send({ type: "watcher_action", action, arguments: arguments_ })} files={files} uploadFiles={uploadLibrary} attachFile={attachLibrary} removeFile={file => { if (window.confirm(`Delete ${file.name} from the local library?`)) send({ type: "delete_workspace_file", file_id: file.id, confirm: true }); }} uploading={uploading} skills={skills} categories={categories} selectedSkill={selectedSkill} selectSkill={skill => { setSelectedSkill(skill); send({ type: "get_skill", name: skill.name }); }} createSkill={() => openSkillEditor(undefined, true)} draftSkill={openSkillDraft} editSkill={skill => openSkillEditor(skill)} removeSkill={skill => { if (window.confirm(`Delete user skill ${skill.name}?`)) send({ type: "delete_skill", name: skill.name }); }} mcp={mcp} probeMcp={() => send({ type: "probe_mcp_servers" })} addMcp={() => openMcpEditor()} editMcp={openMcpEditor} reconnectMcp={server => send({ type: "reconnect_mcp_server", name: server.name })} removeMcp={server => { if (window.confirm(`Remove MCP server ${server.name}? Its tools immediately disappear from Ares and watchers.`)) send({ type: "delete_mcp_server", name: server.name, confirm: true }); }} onSectionChange={section => { if (section === "watchers") send({ type: "get_watcher_state" }); if (section === "files") send({ type: "list_workspace_files" }); if (section === "skills") send({ type: "list_skills" }); if (section === "mcp") send({ type: "get_mcp_state" }); }} close={() => navigate("chat")} />}
      </main>
    </div>
    <Modal state={modal} close={() => setModal(null)} />
    <ArtifactViewer artifact={artifactPreview} close={() => setArtifactPreview(null)} />
    <div className="toast-stack" aria-live="polite">{toasts.map(item => <div className={`toast ${item.tone || ""} ${item.sessionId ? "is-actionable" : ""}`} key={item.id} role={item.sessionId ? "button" : undefined} tabIndex={item.sessionId ? 0 : undefined} onClick={() => { if (item.sessionId) { loadSession(item.sessionId); setToasts(current => current.filter(toastItem => toastItem.id !== item.id)); } }}><i /><span><strong>{item.title}</strong><small>{item.copy}</small></span><button onClick={event => { event.stopPropagation(); setToasts(current => current.filter(toastItem => toastItem.id !== item.id)); }}><X size={13} /></button></div>)}</div>
  </>;
}
