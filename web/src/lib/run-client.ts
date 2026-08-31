"use client";

import { api } from "./api";

export interface RunResult {
	command: string;
	cwd: string;
	platform: NodeJS.Platform | string;
	shell: string;
	stdout: string;
	stderr: string;
	exitCode: number | null;
	timedOut: boolean;
	aborted: boolean;
	durationMs: number;
}

export interface RunError {
	error: string;
	aborted?: boolean;
}

/** Run a shell command via the server-side executor. */
export async function runCommand(args: {
	command: string;
	cwd?: string;
	timeoutMs?: number;
	signal?: AbortSignal;
}): Promise<RunResult> {
	const res = await fetch("/api/run", {
		method: "POST",
		headers: { "content-type": "application/json" },
		body: JSON.stringify({ command: args.command, cwd: args.cwd, timeoutMs: args.timeoutMs }),
		signal: args.signal,
	});
	const data = await res.json().catch(() => ({} as RunError));
	if (!res.ok) {
		const err = data as RunError;
		throw new Error(err.error || `Run failed (HTTP ${res.status})`);
	}
	return data as RunResult;
}

export interface WorkspaceInfo {
	cwd: string;
	platform: string;
	shell: string;
}

export async function getWorkspaceInfo(): Promise<WorkspaceInfo> {
	return api.get<WorkspaceInfo>("/api/workspace/cwd");
}

/** Fire-and-forget: ask the server to spawn a new native terminal. */
export async function revealInTerminal(cwd?: string): Promise<{ ok: boolean; platform: string; cwd: string }> {
	return api.post<{ ok: boolean; platform: string; cwd: string }>("/api/workspace/reveal", { cwd });
}
