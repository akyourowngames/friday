import { useCallback, useEffect } from "react";
import { aresSocket } from "../lib/websocket.js";
import { useChatStore } from "../stores/chatStore.js";
import { useSessionStore } from "../stores/sessionStore.js";
import { useSettingsStore } from "../stores/settingsStore.js";

export function useWebSocket() {
  const addUserMessage = useChatStore((state) => state.addUserMessage);
  const startAssistantMessage = useChatStore((state) => state.startAssistantMessage);
  const appendAssistantContent = useChatStore((state) => state.appendAssistantContent);
  const addToolStart = useChatStore((state) => state.addToolStart);
  const addToolResult = useChatStore((state) => state.addToolResult);
  const finishAssistant = useChatStore((state) => state.finishAssistant);
  const addError = useChatStore((state) => state.addError);
  const loadHistory = useChatStore((state) => state.loadHistory);
  const clearChat = useChatStore((state) => state.clear);
  const activeSessionId = useSessionStore((state) => state.activeSessionId);
  const setSessions = useSessionStore((state) => state.setSessions);
  const setActiveSessionId = useSessionStore((state) => state.setActiveSessionId);
  const connected = useSettingsStore((state) => state.connected);
  const setConnected = useSettingsStore((state) => state.setConnected);
  const setServerUrl = useSettingsStore((state) => state.setServerUrl);
  const setStatus = useSettingsStore((state) => state.setStatus);
  const setModelState = useSettingsStore((state) => state.setModel);
  const setLastError = useSettingsStore((state) => state.setLastError);

  useEffect(() => {
    let cancelled = false;

    const offOpen = aresSocket.on("open", ({ serverUrl }) => {
      setConnected(true);
      setServerUrl(serverUrl);
      setLastError("");
    });
    const offClose = aresSocket.on("close", () => setConnected(false));
    const offError = aresSocket.on("socket_error", ({ message }) => {
      setLastError(message);
    });
    const offContent = aresSocket.on("content", ({ text }) => appendAssistantContent(text));
    const offToolStart = aresSocket.on("tool_start", ({ tool, args }) =>
      addToolStart(tool, args)
    );
    const offToolResult = aresSocket.on("tool_result", ({ tool, content }) =>
      addToolResult(tool, content)
    );
    const offDone = aresSocket.on("response_done", ({ content, tool_calls }) =>
      finishAssistant(content, tool_calls || [])
    );
    const offServerError = aresSocket.on("error", ({ message }) => {
      setLastError(message);
      addError(message);
    });
    const offSessions = aresSocket.on("sessions", ({ sessions }) => setSessions(sessions));
    const offSessionInfo = aresSocket.on("session_info", ({ session_id, model }) => {
      setActiveSessionId(session_id);
      if (model) {
        setModelState(model);
      }
    });
    const offHistory = aresSocket.on("session_history", ({ session_id, messages }) => {
      setActiveSessionId(session_id);
      loadHistory(messages);
    });
    const offStatus = aresSocket.on("status", (payload) => setStatus(payload));
    const offModel = aresSocket.on("model_updated", ({ model }) => setModelState(model));

    async function connect() {
      const serverUrl = window.aresDesktop
        ? await window.aresDesktop.getServerUrl()
        : "ws://127.0.0.1:8765";
      if (!cancelled) {
        aresSocket.connect(serverUrl);
      }
    }

    connect();

    return () => {
      cancelled = true;
      offOpen();
      offClose();
      offError();
      offContent();
      offToolStart();
      offToolResult();
      offDone();
      offServerError();
      offSessions();
      offSessionInfo();
      offHistory();
      offStatus();
      offModel();
    };
  }, [
    addError,
    addToolResult,
    addToolStart,
    appendAssistantContent,
    finishAssistant,
    loadHistory,
    setActiveSessionId,
    setConnected,
    setLastError,
    setModelState,
    setServerUrl,
    setSessions,
    setStatus,
  ]);

  const sendMessage = useCallback(
    (content) => {
      const trimmed = content.trim();
      if (!trimmed) {
        return false;
      }
      addUserMessage(trimmed);
      startAssistantMessage();
      return aresSocket.send({
        type: "chat",
        content: trimmed,
        session_id: activeSessionId
      });
    },
    [activeSessionId, addUserMessage, startAssistantMessage]
  );

  const newSession = useCallback(() => {
    const state = useChatStore.getState();
    if (state.messages.length === 0) {
      return;
    }
    clearChat();
    setActiveSessionId(null);
    aresSocket.send({ type: "new_session" });
  }, [clearChat, setActiveSessionId]);

  const loadSession = useCallback((sessionId) => {
    aresSocket.send({ type: "load_session", session_id: sessionId });
  }, []);

  const setModel = useCallback((model) => {
    aresSocket.send({ type: "set_model", model });
  }, []);

  const refreshSidebar = useCallback(() => {
    aresSocket.send({ type: "list_sessions" });
    aresSocket.send({ type: "get_status" });
  }, []);

  const renameSession = useCallback((sessionId, title) => {
    aresSocket.send({ type: "rename_session", session_id: sessionId, title });
  }, []);

  const deleteSession = useCallback((sessionId) => {
    aresSocket.send({ type: "delete_session", session_id: sessionId });
  }, []);

  const reconnect = useCallback(async () => {
    const serverUrl = window.aresDesktop
      ? await window.aresDesktop.restartServer()
      : "ws://127.0.0.1:8765";
    aresSocket.connect(serverUrl);
  }, []);

  return {
    connected,
    sendMessage,
    newSession,
    loadSession,
    setModel,
    refreshSidebar,
    reconnect,
    renameSession,
    deleteSession
  };
}
