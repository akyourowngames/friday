import "server-only";

/**
 * In-process registry of live PTY-backed terminal sessions for the
 * embedded terminal panel. Each terminal is a real shell (PowerShell on
 * Windows, $SHELL on POSIX) spawned via node-pty, so interactive programs,
 * colors, and cursor control all work exactly like a native terminal.
 *
 * Terminal output is fanned out to subscribers (the SSE stream route);
 * input and resize writes come in via POST. Terminals intentionally
 * outlive a single SSE connection so a background `npm run dev` keeps
 * running when the panel is closed and reopened.
 */

export interface TerminalSession {
	id: string;
	pty: {
		write: (data: string) => void;
		resize: (cols: number, rows: number) => void;
		kill: () => void;
		onData: (cb: (data: string) => void) => { dispose: () => void };
		onExit: (cb: (e: { exitCode: number; pid?: number }) => void) => { dispose: () => void };
		pid: number;
	};
	subs: Set<(data: string) => void>;
	exitSubs: Set<(info: { exitCode: number }) => void>;
	cwd: string;
	shell: string;
	exited: boolean;
}

const terminals = new Map<string, TerminalSession>();

export function createTerminal(cwd?: string): TerminalSession {
	// eslint-disable-next-line @typescript-eslint/no-require-imports
	const pty = require("node-pty") as typeof import("node-pty");
	const shell = process.platform === "win32" ? "powershell.exe" : (process.env.SHELL ?? "/bin/bash");
	const workdir = cwd || process.cwd();
	const ptyProcess = pty.spawn(shell, [], {
		name: "xterm-256color",
		cols: 80,
		rows: 24,
		cwd: workdir,
		env: process.env as { [key: string]: string },
	});

	const session: TerminalSession = {
		id: crypto.randomUUID(),
		pty: ptyProcess,
		subs: new Set(),
		exitSubs: new Set(),
		cwd: workdir,
		shell,
		exited: false,
	};

	ptyProcess.onData((data) => {
		for (const sub of session.subs) sub(data);
	});
	ptyProcess.onExit(({ exitCode }) => {
		session.exited = true;
		for (const sub of session.exitSubs) sub({ exitCode });
		// Give the UI a moment before dropping the handle.
		setTimeout(() => terminals.delete(session.id), 30_000).unref?.();
	});

	terminals.set(session.id, session);
	return session;
}

export function getTerminal(id: string): TerminalSession | undefined {
	return terminals.get(id);
}

export function killTerminal(id: string): boolean {
	const session = terminals.get(id);
	if (!session || session.exited) return false;
	try {
		session.pty.kill();
	} catch {
		// already dead
	}
	terminals.delete(id);
	return true;
}

/** Total live terminal count — used by the health endpoint / tests. */
export function terminalCount(): number {
	return terminals.size;
}
