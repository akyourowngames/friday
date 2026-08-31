"use client";

import { Check, ChevronDown, Copy, Terminal as TerminalIcon, X } from "lucide-react";
import type { RunResult } from "@/lib/run-client";

/**
 * A card that renders a single Quick-Run invocation + its captured output.
 * Status dot uses the same harness status-dot styling as ToolCard so the
 * transcript reads consistently.
 */
export function RunCard({
	command,
	cwd,
	platform,
	shell,
	stdout,
	stderr,
	exitCode,
	timedOut,
	aborted,
	durationMs,
	status,
}: RunResult & { status: "running" | "done" | "error" }) {
	const onCopy = (text: string, e: React.MouseEvent) => {
		e.stopPropagation();
		if (typeof navigator === "undefined" || !navigator.clipboard) return;
		navigator.clipboard.writeText(text).catch(() => undefined);
	};

	const exitBadge = (() => {
		if (aborted) return { label: "aborted", cls: "is-error" };
		if (timedOut) return { label: "timed out", cls: "is-error" };
		if (exitCode === 0) return { label: "exit 0", cls: "is-done" };
		return { label: `exit ${exitCode ?? "?"}`, cls: "is-error" };
	})();

	return (
		<section className={`harness-tool-card is-shell ${status === "running" ? "" : ""}`}>
			<header className="harness-run-head">
				<span className={`harness-status-dot is-${status}`} />
				<TerminalIcon size={15} strokeWidth={1.8} />
				<span className="harness-run-title">Quick Run</span>
				<code className="harness-run-cmd">{command}</code>
				<span className={`harness-run-badge ${exitBadge.cls}`}>{exitBadge.label}</span>
				<span className="harness-run-meta">{durationMs}ms · {shell}</span>
			</header>
			{status === "running" ? (
				<div className="harness-run-running">
					<span className="harness-pulse-dot" /> running in {cwd}
				</div>
			) : (
				<div className="harness-run-output">
					{stdout && (
						<div className="harness-run-block">
							<div className="harness-run-block-head">
								<span>stdout</span>
								<button
									type="button"
									onClick={(e) => onCopy(stdout, e)}
									title="Copy stdout"
									aria-label="Copy stdout"
								>
									<Copy size={12} />
								</button>
							</div>
							<pre>{stdout}</pre>
						</div>
					)}
					{stderr && (
						<div className="harness-run-block is-stderr">
							<div className="harness-run-block-head">
								<span>stderr</span>
								<button
									type="button"
									onClick={(e) => onCopy(stderr, e)}
									title="Copy stderr"
									aria-label="Copy stderr"
								>
									<Copy size={12} />
								</button>
							</div>
							<pre>{stderr}</pre>
						</div>
					)}
					{!stdout && !stderr && (
						<div className="harness-run-block">
							<pre className="harness-run-empty">(no output)</pre>
						</div>
					)}
					<div className="harness-run-foot">
						<code>{cwd}</code>
						<span> · {platform}</span>
					</div>
				</div>
			)}
		</section>
	);
}
