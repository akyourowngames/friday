"use client";

import { useEffect, useState } from "react";
import { History, Plus, Search, Settings2, Sun, Terminal as TerminalIcon } from "lucide-react";
import { revealInTerminal } from "@/lib/run-client";
import { toast } from "sonner";

export function CommandPalette({
	onClose,
	actions,
}: {
	onClose: () => void;
	actions: {
		newSession: () => void;
		openSettings: () => void;
		toggleTheme: () => void;
		toggleTerminal?: () => void;
		openTimeTravel?: () => void;
	};
}) {
	const [query, setQuery] = useState("");

	useEffect(() => {
		const onKey = (e: KeyboardEvent) => {
			if (e.key === "Escape") onClose();
		};
		window.addEventListener("keydown", onKey);
		return () => window.removeEventListener("keydown", onKey);
	}, [onClose]);

	const openTerminal = async () => {
		try {
			const result = await revealInTerminal();
			toast.success(`Opened terminal in ${result.cwd}`);
		} catch (e) {
			toast.error("Could not open terminal", { description: (e as Error).message });
		}
	};

	const items = [
		{ label: "New session", icon: Plus, run: actions.newSession },
		{ label: "Open settings", icon: Settings2, run: actions.openSettings },
		{ label: "Open terminal", icon: TerminalIcon, run: openTerminal },
		...(actions.toggleTerminal
			? [{ label: "Toggle embedded terminal", icon: TerminalIcon, run: actions.toggleTerminal }]
			: []),
		...(actions.openTimeTravel
			? [{ label: "Time travel (restore checkpoints)", icon: History, run: actions.openTimeTravel }]
			: []),
		{ label: "Toggle theme", icon: Sun, run: actions.toggleTheme },
	];
	const filtered = items.filter((item) => item.label.toLowerCase().includes(query.toLowerCase()));

	return (
		<div className="harness-modal-backdrop" onMouseDown={onClose}>
			<section
				className="harness-modal harness-palette"
				onMouseDown={(e) => e.stopPropagation()}
				role="dialog"
				aria-label="Command palette"
			>
				<div className="harness-palette-search">
					<Search size={17} />
					<input
						autoFocus
						placeholder="Type a command or search…"
						value={query}
						onChange={(e) => setQuery(e.target.value)}
					/>
				</div>
				<div className="harness-palette-actions">
					<p>Quick actions</p>
					{filtered.map((item) => {
						const Icon = item.icon;
						return (
							<button
								key={item.label}
								type="button"
								onClick={() => {
									void item.run();
									onClose();
								}}
							>
								<Icon size={16} />
								{item.label}
							</button>
						);
					})}
				</div>
			</section>
		</div>
	);
}
