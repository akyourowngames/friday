"use client";

import { useCallback, useEffect, useState } from "react";
import { History, Undo2, X } from "lucide-react";
import { toast } from "sonner";

interface CheckpointSummary {
	id: string;
	createdAt: string;
	status: string;
	toolName?: string;
	restoredAt?: string;
	entryCount?: number;
}

/**
 * Time-travel dialog — lists the active session's checkpoints and lets the
 * user restore the workspace to any of them (files snap back to their
 * pre-tool state; files created after the checkpoint are removed).
 */
export function TimeTravelPanel({
	sessionId,
	onClose,
}: {
	sessionId: string | null;
	onClose: () => void;
}) {
	const [checkpoints, setCheckpoints] = useState<CheckpointSummary[]>([]);
	const [loading, setLoading] = useState(false);
	const [restoring, setRestoring] = useState<string | null>(null);

	const load = useCallback(async () => {
		if (!sessionId) return;
		setLoading(true);
		try {
			const res = await fetch(`/api/checkpoints?sessionId=${encodeURIComponent(sessionId)}`);
			const json = (await res.json()) as { checkpoints?: CheckpointSummary[] };
			setCheckpoints(json.checkpoints ?? []);
		} catch {
			toast.error("Could not load checkpoints");
		} finally {
			setLoading(false);
		}
	}, [sessionId]);

	useEffect(() => {
		void load();
	}, [load]);

	// Esc closes the modal
	useEffect(() => {
		const onKey = (e: KeyboardEvent) => {
			if (e.key === "Escape") onClose();
		};
		window.addEventListener("keydown", onKey);
		return () => window.removeEventListener("keydown", onKey);
	}, [onClose]);

	const restore = useCallback(
		async (checkpointId: string) => {
			if (!sessionId) return;
			setRestoring(checkpointId);
			try {
				const res = await fetch("/api/checkpoints", {
					method: "POST",
					headers: { "content-type": "application/json" },
					body: JSON.stringify({ sessionId, checkpointId }),
				});
				const json = (await res.json()) as { restored?: string[]; deleted?: string[]; error?: string };
				if (!res.ok) throw new Error(json.error ?? "Restore failed");
				toast.success(`Restored ${(json.restored ?? []).length} file(s), removed ${(json.deleted ?? []).length}`);
				void load();
			} catch (error) {
				toast.error(error instanceof Error ? error.message : "Restore failed");
			} finally {
				setRestoring(null);
			}
		},
		[sessionId, load],
	);

	return (
		<div className="harness-modal-backdrop" onMouseDown={onClose}>
			<section
				className="harness-modal harness-timetravel"
				onMouseDown={(event) => event.stopPropagation()}
				role="dialog"
				aria-label="Time travel"
			>
				<header className="harness-modal-header">
					<h2>
						<History size={16} /> Time travel
					</h2>
					<button type="button" className="harness-icon-button" onClick={onClose} aria-label="Close">
						<X size={16} />
					</button>
				</header>
				<div className="harness-modal-body">
					{!sessionId && (
						<p className="harness-timetravel-empty">Start a conversation first — checkpoints are created before every file-touching tool call.</p>
					)}
					{sessionId && loading && <p className="harness-timetravel-empty">Loading checkpoints…</p>}
					{sessionId && !loading && checkpoints.length === 0 && (
						<p className="harness-timetravel-empty">
							No checkpoints yet in this session. Every <code>write</code>, <code>edit</code>, and <code>bash</code> call creates one automatically.
						</p>
					)}
					<ul className="harness-timetravel-list">
						{checkpoints.map((checkpoint) => (
							<li key={checkpoint.id} className="harness-timetravel-item">
								<div className="harness-timetravel-meta">
									<span className="harness-timetravel-tool">{checkpoint.toolName ?? "tool"}</span>
									<span className="harness-timetravel-date">
										{new Date(checkpoint.createdAt).toLocaleString()}
									</span>
									{checkpoint.restoredAt && <span className="harness-timetravel-restored">restored</span>}
								</div>
								<button
									type="button"
									className="harness-timetravel-restore"
									disabled={restoring === checkpoint.id}
									onClick={() => void restore(checkpoint.id)}
								>
									<Undo2 size={13} />
									{restoring === checkpoint.id ? "Restoring…" : "Restore"}
								</button>
							</li>
						))}
					</ul>
				</div>
			</section>
		</div>
	);
}
