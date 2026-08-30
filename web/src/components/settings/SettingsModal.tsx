"use client";

import { useEffect, useState } from "react";
import { X } from "lucide-react";
import type { SettingSchema, SettingValue } from "@/lib/types";

export function SettingsModal({
	settings,
	schema,
	onSave,
	onClose,
}: {
	settings: Record<string, unknown>;
	schema: SettingSchema[];
	onSave: (next: Record<string, unknown>) => Promise<void>;
	onClose: () => void;
}) {
	const [draft, setDraft] = useState<Record<string, unknown>>(settings);
	const [speakReplies, setSpeakReplies] = useState(false);

	useEffect(() => {
		setDraft(settings);
	}, [settings]);

	// Esc closes the modal
	useEffect(() => {
		const onKey = (e: KeyboardEvent) => {
			if (e.key === "Escape") onClose();
		};
		window.addEventListener("keydown", onKey);
		return () => window.removeEventListener("keydown", onKey);
	}, [onClose]);

	const update = async (key: string, value: SettingValue) => {
		const next = { ...draft, [key]: value };
		setDraft(next);
		await onSave(next);
	};

	return (
		<div className="harness-modal-backdrop" onMouseDown={onClose}>
			<section className="harness-modal" onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-label="Settings">
				<header className="harness-modal-header">
					<div>
						<p className="harness-eyebrow">HARNESS CONFIGURATION</p>
						<h2>Settings</h2>
					</div>
					<button type="button" className="harness-icon-button" onClick={onClose} aria-label="Close">
						<X size={18} />
					</button>
				</header>
				<div className="harness-settings-list">
					<div className="harness-setting-row">
						<div>
							<strong>Always speak replies</strong>
							<p>Read new assistant replies aloud.</p>
						</div>
						<button
							type="button"
							className={`harness-switch ${speakReplies ? "is-on" : ""}`}
							onClick={() => setSpeakReplies((v) => !v)}
							aria-pressed={speakReplies}
							aria-label="Always speak replies"
						>
							<i />
						</button>
					</div>
					{schema.map((item) => (
						<div className="harness-setting-row" key={item.key}>
							<div>
								<strong>{item.label}</strong>
								<p>{item.description}</p>
							</div>
							{item.type === "boolean" ? (
								<button
									type="button"
									className={`harness-switch ${draft[item.key] ? "is-on" : ""}`}
									onClick={() => void update(item.key, !draft[item.key])}
									aria-pressed={Boolean(draft[item.key])}
									aria-label={item.label}
								>
									<i />
								</button>
							) : item.type === "enum" ? (
								<select
									value={(draft[item.key] as string) ?? item.options?.[0] ?? ""}
									onChange={(e) => void update(item.key, e.target.value)}
								>
									{item.options?.map((opt) => (
										<option key={opt} value={opt}>
											{opt}
										</option>
									))}
								</select>
							) : (
								<input
									type="number"
									min={item.min}
									max={item.max}
									value={(draft[item.key] as number | undefined) ?? ""}
									onChange={(e) => {
										const n = e.target.value === "" ? null : Number(e.target.value);
										void update(item.key, n);
									}}
								/>
							)}
						</div>
					))}
				</div>
			</section>
		</div>
	);
}
