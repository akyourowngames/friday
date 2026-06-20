import { create } from "zustand";

export const MODEL_REGISTRY = {
  free: {
    label: "Free Models",
    models: [
      { id: "deepseek-v4-flash-free", label: "DeepSeek V4 Flash", provider: "DeepSeek" },
      { id: "mimo-v2.5-free", label: "MiMo V2.5", provider: "MiMo" },
      { id: "qwen3.6-plus-free", label: "Qwen 3.6 Plus", provider: "Qwen" },
      { id: "minimax-m3-free", label: "MiniMax M3", provider: "MiniMax" },
      { id: "nemotron-3-ultra-free", label: "Nemotron 3 Ultra", provider: "NVIDIA" },
      { id: "north-mini-code-free", label: "North Mini Code", provider: "North" },
      { id: "big-pickle", label: "Big Pickle", provider: "OpenCode" },
    ],
  },
  claude: {
    label: "Claude",
    models: [
      { id: "claude-fable-5", label: "Fable 5", provider: "Anthropic" },
      { id: "claude-opus-4-8", label: "Opus 4.8", provider: "Anthropic" },
      { id: "claude-opus-4-7", label: "Opus 4.7", provider: "Anthropic" },
      { id: "claude-opus-4-6", label: "Opus 4.6", provider: "Anthropic" },
      { id: "claude-opus-4-5", label: "Opus 4.5", provider: "Anthropic" },
      { id: "claude-opus-4-1", label: "Opus 4.1", provider: "Anthropic" },
      { id: "claude-sonnet-4-6", label: "Sonnet 4.6", provider: "Anthropic" },
      { id: "claude-sonnet-4-5", label: "Sonnet 4.5", provider: "Anthropic" },
      { id: "claude-sonnet-4", label: "Sonnet 4", provider: "Anthropic" },
      { id: "claude-haiku-4-5", label: "Haiku 4.5", provider: "Anthropic" },
    ],
  },
  gpt: {
    label: "GPT",
    models: [
      { id: "gpt-5.5", label: "GPT-5.5", provider: "OpenAI" },
      { id: "gpt-5.5-pro", label: "GPT-5.5 Pro", provider: "OpenAI" },
      { id: "gpt-5.4", label: "GPT-5.4", provider: "OpenAI" },
      { id: "gpt-5.4-pro", label: "GPT-5.4 Pro", provider: "OpenAI" },
      { id: "gpt-5.4-mini", label: "GPT-5.4 Mini", provider: "OpenAI" },
      { id: "gpt-5.4-nano", label: "GPT-5.4 Nano", provider: "OpenAI" },
      { id: "gpt-5.3-codex-spark", label: "GPT-5.3 Codex Spark", provider: "OpenAI" },
      { id: "gpt-5.3-codex", label: "GPT-5.3 Codex", provider: "OpenAI" },
      { id: "gpt-5.2", label: "GPT-5.2", provider: "OpenAI" },
      { id: "gpt-5.2-codex", label: "GPT-5.2 Codex", provider: "OpenAI" },
      { id: "gpt-5.1", label: "GPT-5.1", provider: "OpenAI" },
      { id: "gpt-5.1-codex-max", label: "GPT-5.1 Codex Max", provider: "OpenAI" },
      { id: "gpt-5.1-codex", label: "GPT-5.1 Codex", provider: "OpenAI" },
      { id: "gpt-5.1-codex-mini", label: "GPT-5.1 Codex Mini", provider: "OpenAI" },
      { id: "gpt-5", label: "GPT-5", provider: "OpenAI" },
      { id: "gpt-5-codex", label: "GPT-5 Codex", provider: "OpenAI" },
      { id: "gpt-5-nano", label: "GPT-5 Nano", provider: "OpenAI" },
    ],
  },
  gemini: {
    label: "Gemini",
    models: [
      { id: "gemini-3.5-flash", label: "Gemini 3.5 Flash", provider: "Google" },
      { id: "gemini-3.1-pro", label: "Gemini 3.1 Pro", provider: "Google" },
      { id: "gemini-3-flash", label: "Gemini 3 Flash", provider: "Google" },
    ],
  },
  other: {
    label: "Other Models",
    models: [
      { id: "grok-build-0.1", label: "Grok Build 0.1", provider: "xAI" },
      { id: "deepseek-v4-pro", label: "DeepSeek V4 Pro", provider: "DeepSeek" },
      { id: "deepseek-v4-flash", label: "DeepSeek V4 Flash", provider: "DeepSeek" },
      { id: "glm-5.1", label: "GLM 5.1", provider: "Zhipu" },
      { id: "glm-5", label: "GLM 5", provider: "Zhipu" },
      { id: "minimax-m2.7", label: "MiniMax M2.7", provider: "MiniMax" },
      { id: "minimax-m2.5", label: "MiniMax M2.5", provider: "MiniMax" },
      { id: "kimi-k2.6", label: "Kimi K2.6", provider: "Moonshot" },
      { id: "kimi-k2.5", label: "Kimi K2.5", provider: "Moonshot" },
      { id: "qwen3.6-plus", label: "Qwen 3.6 Plus", provider: "Qwen" },
      { id: "qwen3.5-plus", label: "Qwen 3.5 Plus", provider: "Qwen" },
    ],
  },
};

// Flat list for backward compatibility
export const KNOWN_MODELS = Object.values(MODEL_REGISTRY).flatMap(
  (group) => group.models.map((m) => m.id)
);

export const useSettingsStore = create((set) => ({
  connected: false,
  serverUrl: "",
  model: "deepseek-v4-flash-free",
  memoryCount: 0,
  taskCount: 0,
  autoExecCount: 0,
  settingsOpen: false,
  lastError: "",
  taskNotifications: [],
  executorState: "unknown",
  executorCurrentTask: null,
  executorTasksCompleted: 0,
  executorTasksFailed: 0,

  setConnected(connected) {
    set({ connected });
  },

  setServerUrl(serverUrl) {
    set({ serverUrl });
  },

  setStatus(status) {
    set({
      model: status.model || "deepseek-v4-flash-free",
      memoryCount: status.memory_count ?? 0,
      taskCount: status.task_count ?? 0,
      autoExecCount: status.auto_exec_count ?? 0,
      executorState: status.executor_state || "unknown",
      executorCurrentTask: status.executor_current_task || null,
      executorTasksCompleted: status.executor_tasks_completed ?? 0,
      executorTasksFailed: status.executor_tasks_failed ?? 0
    });
  },

  addTaskNotification(notification) {
    const id = `task-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    set((state) => ({
      taskNotifications: [...state.taskNotifications, { ...notification, id }]
    }));
    setTimeout(() => {
      set((state) => ({
        taskNotifications: state.taskNotifications.filter((n) => n.id !== id)
      }));
    }, 12000);
  },

  dismissTaskNotification(id) {
    set((state) => ({
      taskNotifications: state.taskNotifications.filter((n) => n.id !== id)
    }));
  },

  setModel(model) {
    set({ model });
  },

  setSettingsOpen(settingsOpen) {
    set({ settingsOpen });
  },

  setLastError(lastError) {
    set({ lastError });
  }
}));
