"use client";

import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { X } from "lucide-react";
import "@xterm/xterm/css/xterm.css";
import { parseSse } from "@/lib/sse";

/**
 * Embedded terminal panel — a real PTY-backed shell (PowerShell on Windows)
 * rendered with xterm.js. Output streams over SSE from /api/terminal/[id];
 * keystrokes and resizes go back via POST. Closing the panel leaves the
 * shell running server-side; reopening attaches to a fresh shell.
 */
export function TerminalPanel({ cwd, onClose }: { cwd?: string; onClose: () => void }) {
	const hostRef = useRef<HTMLDivElement>(null);

	useEffect(() => {
		const host = hostRef.current;
		if (!host) return;

		let disposed = false;
		const term = new Terminal({
			fontSize: 13,
			fontFamily: "Consolas, 'Cascadia Mono', monospace",
			cursorBlink: true,
			theme: {
				background: "#0b0e14",
				foreground: "#c8d3f5",
				cursor: "#82aaff",
			},
		});
		const fit = new FitAddon();
		term.loadAddon(fit);
		term.open(host);
		try {
			fit.fit();
		} catch {
			// host not laid out yet — resize handler will fit later
		}

		let abortController: AbortController | null = null;
		let resizeObserver: ResizeObserver | null = null;

		const post = (body: Record<string, unknown>, terminalId: string) => {
			void fetch(`/api/terminal/${terminalId}/input`, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify(body),
			}).catch(() => undefined);
		};

		(async () => {
			try {
				const createRes = await fetch("/api/terminal", {
					method: "POST",
					headers: { "content-type": "application/json" },
					body: JSON.stringify({ cwd, cols: term.cols, rows: term.rows }),
				});
				if (!createRes.ok) throw new Error(`spawn failed (${createRes.status})`);
				const { id } = (await createRes.json()) as { id: string };
				if (disposed) return;

				term.onData((data) => post({ input: data }, id));

				resizeObserver = new ResizeObserver(() => {
					try {
						fit.fit();
						post({ cols: term.cols, rows: term.rows }, id);
					} catch {
						// not attached yet
					}
				});
				resizeObserver.observe(host);

				abortController = new AbortController();
				const streamRes = await fetch(`/api/terminal/${id}`, { signal: abortController.signal });
				if (!streamRes.ok || !streamRes.body) throw new Error(`stream failed (${streamRes.status})`);
				for await (const frame of parseSse(streamRes.body, abortController.signal)) {
					if (frame.event === "data" && typeof (frame.data as { text?: string }).text === "string") {
						term.write((frame.data as { text: string }).text);
					} else if (frame.event === "exit") {
						term.write("\r\n\x1b[2m[process exited]\x1b[0m\r\n");
					}
				}
			} catch (error) {
				if (!disposed) {
					term.write(`\r\n\x1b[31mterminal error: ${error instanceof Error ? error.message : "unknown"}\x1b[0m\r\n`);
				}
			}
		})();

		return () => {
			disposed = true;
			abortController?.abort();
			resizeObserver?.disconnect();
			term.dispose();
		};
	}, [cwd]);

	return (
		<div className="harness-terminal-panel" data-testid="terminal-panel">
			<div className="harness-terminal-panel-head">
				<span className="harness-terminal-panel-title">
					<span className="harness-live-dot" /> Terminal
				</span>
				<button
					type="button"
					className="harness-icon-button"
					onClick={onClose}
					aria-label="Close terminal"
				>
					<X size={16} />
				</button>
			</div>
			<div ref={hostRef} className="harness-terminal-host" />
		</div>
	);
}
