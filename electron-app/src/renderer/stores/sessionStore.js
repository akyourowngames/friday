import { create } from "zustand";

export const useSessionStore = create((set, get) => ({
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
  },

  filteredSessions() {
    const query = get().search.trim().toLowerCase();
    if (!query) {
      return get().sessions;
    }
    return get().sessions.filter((session) =>
      `${session.title || ""} ${session.summary || ""}`.toLowerCase().includes(query)
    );
  }
}));
