import { create } from "zustand";

function id(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function normalizeToolCall(call) {
  const safeCall = call && typeof call === "object" ? call : {};
  return {
    id: safeCall.id || `tool-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    tool: safeCall.tool || safeCall.tool_name || safeCall.name || "unknown",
    args: safeCall.args || safeCall.arguments || {},
    content: safeCall.content ?? null,
    status: safeCall.status || "done",
    opened: safeCall.opened ?? false,
  };
}

function normalizeToolCalls(message) {
  const raw = message.toolCalls ?? message.tool_calls ?? [];
  if (Array.isArray(raw)) {
    return raw.map(normalizeToolCall);
  }
  if (typeof raw === "string" && raw.trim()) {
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.map(normalizeToolCall) : [];
    } catch {
      return [];
    }
  }
  return [];
}

function normalizeMessage(message) {
  return {
    id: message.id || id(message.role || "message"),
    role: message.role || "assistant",
    content: message.content || "",
    createdAt: message.created_at || message.createdAt || new Date().toISOString(),
    toolCalls: normalizeToolCalls(message),
    status: message.status || "done"
  };
}

export const useChatStore = create((set, get) => ({
  messages: [],
  activeAssistantId: null,
  isStreaming: false,
  error: "",

  addUserMessage(content) {
    set((state) => ({
      error: "",
      messages: [
        ...state.messages,
        normalizeMessage({ role: "user", content, status: "done" })
      ]
    }));
  },

  startAssistantMessage() {
    const message = normalizeMessage({
      role: "assistant",
      content: "",
      status: "streaming",
      toolCalls: []
    });
    set((state) => ({
      activeAssistantId: message.id,
      isStreaming: true,
      messages: [...state.messages, message]
    }));
  },

  appendAssistantContent(text) {
    const { activeAssistantId } = get();
    if (!activeAssistantId) {
      get().startAssistantMessage();
    }
    const currentId = get().activeAssistantId;
    set((state) => ({
      messages: state.messages.map((message) =>
        message.id === currentId
          ? { ...message, content: `${message.content}${text}`, status: "streaming" }
          : message
      )
    }));
  },

  addToolStart(tool, args) {
    const { activeAssistantId } = get();
    if (!activeAssistantId) {
      get().startAssistantMessage();
    }
    const currentId = get().activeAssistantId;
    const toolCall = {
      id: id("tool"),
      tool,
      args: args || {},
      content: null,
      status: "running",
      opened: true
    };
    set((state) => ({
      messages: state.messages.map((message) =>
        message.id === currentId
          ? { ...message, toolCalls: [...message.toolCalls, toolCall] }
          : message
      )
    }));
  },

  addToolResult(tool, content) {
    const currentId = get().activeAssistantId;
    set((state) => ({
      messages: state.messages.map((message) => {
        if (message.id !== currentId) {
          return message;
        }
        let updated = false;
        const toolCalls = message.toolCalls.map((call) => {
          if (!updated && call.tool === tool && call.status === "running") {
            updated = true;
            return { ...call, content, status: "done" };
          }
          return call;
        });
        return { ...message, toolCalls };
      })
    }));
  },

  finishAssistant(content, toolCalls = []) {
    const currentId = get().activeAssistantId;
    set((state) => ({
      activeAssistantId: null,
      isStreaming: false,
      messages: state.messages.map((message) => {
        if (message.id !== currentId) {
          return message;
        }
        const completedTools = message.toolCalls.length
          ? message.toolCalls
          : toolCalls.map((call) => ({ ...call, id: id("tool"), status: "done" }));
        return {
          ...message,
          content: content || message.content,
          toolCalls: completedTools,
          status: "done"
        };
      })
    }));
  },

  addError(message) {
    set((state) => ({
      error: message,
      activeAssistantId: null,
      isStreaming: false,
      messages: [
        ...state.messages,
        normalizeMessage({ role: "error", content: message, status: "done" })
      ]
    }));
  },

  loadHistory(messages) {
    set({
      messages: (messages || []).map(normalizeMessage),
      activeAssistantId: null,
      isStreaming: false,
      error: ""
    });
  },

  clear() {
    set({ messages: [], activeAssistantId: null, isStreaming: false, error: "" });
  }
}));
