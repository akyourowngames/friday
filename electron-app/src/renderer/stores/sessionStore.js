import { create } from "zustand";

export const useSessionStore = create((set) => ({
  sessions: [],
  activeSessionId: null,
  search: "",

  setSessions(sessions) {
    set({ sessions: sessions || [] });
  },

  setActiveSessionId(activeSessionId) {
    set({ activeSessionId });
  },

  setSearch(search) {
    set({ search });
  }
}));
