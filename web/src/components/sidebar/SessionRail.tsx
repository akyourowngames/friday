"use client";

import { Plus, Search, Settings2, X } from "lucide-react";
import type { SessionMeta } from "@/lib/types";
import { relativeTime } from "@/lib/utils";

export function SessionRail({
	sessions,
	activeSessionId,
	activeProviderName,
	onPick,
	onNew,
	onOpenSettings,
	onClose,
}: {
	sessions: SessionMeta[];
	activeSessionId: string | null;
	activeProviderName: string;
	onPick: (id: string) => void;
	onNew: () => void;
	onOpenSettings: () => void;
	onClose: () => void;
}) {
	return (
		<>
			<div className="harness-brand">
				<button type="button" className="harness-mark" onClick={onNew} aria-label="New session">
					<span />
					<span />
					<span />
				</button>
				<div>
					<strong>HarNESs</strong>
					<small>friday-ng channel</small>
				</div>
				<button
					type="button"
					className="harness-icon-button"
					onClick={onClose}
					aria-label="Close sidebar"
					title="Close sidebar"
				>
					<X size={16} />
				</button>
			</div>

			<button type="button" className="harness-new-session" onClick={onNew}>
				<Plus size={16} />
				New session <kbd>⌘ N</kbd>
			</button>

			<label className="harness-session-search">
				<Search size={15} />
				<input placeholder="Search sessions" />
			</label>

			<div className="harness-session-list">
				<p className="harness-section-label">Recent sessions</p>
				{sessions.length ? (
					sessions.slice(0, 8).map((session) => (
						<button
							key={session.id}
							type="button"
							className={`harness-session-item ${activeSessionId === session.id ? "is-active" : ""}`}
							onClick={() => onPick(session.id)}
						>
							<span>{session.title || "Untitled conversation"}</span>
							<small>{relativeTime(session.updatedAt)}</small>
						</button>
					))
				) : (
					<div className="harness-quiet-empty">Your sessions will live here.</div>
				)}
			</div>

			<div className="harness-rail-footer">
				<div className="harness-provider-indicator">
					<span className="harness-live-dot" />
					<div>
						<small>Provider</small>
						<strong>{activeProviderName}</strong>
					</div>
				</div>
				<button
					type="button"
					className="harness-icon-button"
					onClick={onOpenSettings}
					title="Settings"
					aria-label="Settings"
				>
					<Settings2 size={17} />
				</button>
			</div>
		</>
	);
}
