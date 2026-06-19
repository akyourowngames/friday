import { create } from "zustand";

export const KNOWN_MODELS = [
  "deepseek-v4-flash-free",
  "mimo-v2.5-free",
  "nemotron-3-ultra-free",
  "big-pickle",
  "north-mini-code-free"
];

export const useSettingsStore = create((set) => ({
  connected: false,
  serverUrl: "",
  model: "deepseek-v4-flash-free",
  memoryCount: 0,
  taskCount: 0,
  settingsOpen: false,
  lastError: "",

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
      taskCount: status.task_count ?? 0
    });
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
