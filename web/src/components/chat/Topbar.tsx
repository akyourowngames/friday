"use client";

import { PanelLeftClose, Settings2 } from "lucide-react";
import type { ReactNode } from "react";

export function Topbar({
	isStreaming,
	activeSessionId,
	onToggleRail,
	onOpenSettings,
	themeToggle,
	extra,
}: {
	isStreaming: boolean;
	activeSessionId: string | null;
	onToggleRail: () => void;
	onOpenSettings: () => void;
	themeToggle: ReactNode;
	extra?: ReactNode;
}) {
	return (
		<header className="harness-topbar">
			<div className="harness-topbar-left">
				<button
					type="button"
					className="harness-icon-button"
					onClick={onToggleRail}
					aria-label="Toggle sidebar"
				>
					<PanelLeftClose size={17} />
				</button>
				<span className="harness-connection">
					<span className="harness-live-dot" />
					Connected
				</span>
				<span className="harness-top-divider" />
				<span className="harness-context-name">{activeSessionId ? "Session active" : "New conversation"}</span>
			</div>
			<div className="harness-topbar-actions">
				<button type="button" className="harness-usage">
					{isStreaming ? (
						<>
							<span className="harness-pulse-dot" /> streaming
						</>
					) : (
						"Ready"
					)}
				</button>
				{extra}
				{themeToggle}
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
		</header>
	);
}
