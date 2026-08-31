"use client";

import { useEffect, useRef, useState } from "react";
import { Play, Square, Terminal as TerminalIcon, X } from "lucide-react";
import { runCommand } from "@/lib/run-client";
import type { RunResult } from "@/lib/run-client";

/**
 * Quick Run bar — a thin input above the composer that runs a shell
 * command directly via `/api/run` (no agent round-trip). On submit, the
 * command + result is pushed onto the chat transcript as a `RunCard`
 * via the `onRun` callback, so it sits inline with the conversation
 * and survives a session reload.
 */
export function QuickRunBar({
	cwd,
	disabled,
	onRun,
}: {
	cwd: string;
	disabled?: boolean;
	onRun: (result: RunResult, command: string) => void;
}) {
	const [command, setCommand] = useState("");
	const [running, setRunning] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const controllerRef = useRef<AbortController | null>(null);
	const inputRef = useRef<HTMLInputElement | null>(null);

	useEffect(() => {
		// Keyboard shortcut: Ctrl+Shift+R focuses the quick-run bar.
		const onKey = (e: KeyboardEvent) => {
			if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "r") {
				e.preventDefault();
				inputRef.current?.focus();
			}
		};
		window.addEventListener("keydown", onKey);
		return () => window.removeEventListener("keydown", onKey);
	}, []);

	const submit = async (raw?: string) => {
		const cmd = (raw ?? command).trim();
		if (!cmd || running) return;
		setError(null);
		setRunning(true);
		const controller = new AbortController();
		controllerRef.current = controller;
		try {
			const result = await runCommand({ command: cmd, cwd, signal: controller.signal });
			onRun(result, cmd);
			setCommand("");
		} catch (e) {
			if ((e as Error).name !== "AbortError") {
				setError((e as Error).message);
			}
		} finally {
			controllerRef.current = null;
			setRunning(false);
		}
	};

	const cancel = () => {
		controllerRef.current?.abort();
	};

	return (
		<div className="harness-quickrun">
			<span className="harness-quickrun-prompt" aria-hidden="true">
				<TerminalIcon size={13} />
			</span>
			<input
				ref={inputRef}
				className="harness-quickrun-input"
				type="text"
				spellCheck={false}
				autoComplete="off"
				placeholder={`Run a command in ${cwd}  (Ctrl+Shift+R)`}
				value={command}
				disabled={disabled || running}
				onChange={(e) => {
					setCommand(e.target.value);
					if (error) setError(null);
				}}
				onKeyDown={(e) => {
					if (e.key === "Enter" && !e.shiftKey) {
						e.preventDefault();
						void submit();
					}
				}}
			/>
			{running ? (
				<button
					type="button"
					className="harness-quickrun-btn is-stop"
					onClick={cancel}
					title="Stop (Esc)"
					aria-label="Stop running command"
				>
					<Square size={13} fill="currentColor" />
				</button>
			) : (
				<button
					type="button"
					className="harness-quickrun-btn"
					onClick={() => void submit()}
					disabled={disabled || !command.trim()}
					title="Run (Enter)"
					aria-label="Run command"
				>
					<Play size={13} fill="currentColor" />
				</button>
			)}
			{error && (
				<div className="harness-quickrun-error" role="alert">
					<X size={12} />
					<span>{error}</span>
					<button type="button" onClick={() => setError(null)} aria-label="Dismiss error">
						<X size={12} />
					</button>
				</div>
			)}
		</div>
	);
}
