"use client";

import { useCallback, useState } from "react";
import { api } from "./api";
import type { AgentMessage, SessionMeta } from "./types";

/** Manages the recent-sessions list shown in the sidebar. */
export function useSessions(initial: SessionMeta[]) {
	const [sessions, setSessions] = useState<SessionMeta[]>(initial);

	const refresh = useCallback(async () => {
		try {
			const next = await api.get<SessionMeta[]>("/api/sessions");
			setSessions(next);
		} catch {
			// ignore — sidebar just shows stale data
		}
	}, []);

	const loadSession = useCallback(async (id: string): Promise<{ meta: SessionMeta; messages: AgentMessage[] } | null> => {
		try {
			return await api.get<{ meta: SessionMeta; messages: AgentMessage[] }>(`/api/sessions/${encodeURIComponent(id)}`);
		} catch {
			return null;
		}
	}, []);

	return { sessions, refresh, loadSession, setSessions };
}
