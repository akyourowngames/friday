"use client";

import { useEffect, useMemo, useState } from "react";
import { ExternalLink, Eye, EyeOff, X } from "lucide-react";
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
	const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({});

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

	// Group settings by their `group` field. Settings without a group go
	// into the default "General" bucket so the modal always has at least
	// one section.
	const groups = useMemo(() => {
		const map = new Map<string, SettingSchema[]>();
		for (const item of schema) {
			const name = item.group ?? "General";
			if (!map.has(name)) map.set(name, []);
			map.get(name)!.push(item);
		}
		// Stable order: General first, then API keys, then anything else.
		const order = ["General", "API keys"].filter((g) => map.has(g));
		for (const g of map.keys()) if (!order.includes(g)) order.push(g);
		return order.map((name) => ({ name, items: map.get(name)! }));
	}, [schema]);

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
					{groups.map((group) => (
						<section key={group.name} className="harness-settings-group" aria-labelledby={`group-${group.name}`}>
							<h3 className="harness-settings-group-title" id={`group-${group.name}`}>
								{group.name === "API keys" ? "API keys & search providers" : group.name}
							</h3>
							{group.name === "API keys" && (
								<p className="harness-settings-group-hint">
									Stored locally in <code>~/.friday-ng/config.json</code> under <code>settings</code>. Never sent anywhere except the provider
									they unlock.
								</p>
							)}
							{group.items.map((item) => {
								const isSecret = item.type === "secret";
								const inputType =
									item.type === "number" ? "number" :
									item.type === "secret" ? (showSecrets[item.key] ? "text" : "password") :
									item.type === "url" ? "url" :
									"text";
								return (
									<div className="harness-setting-row" key={item.key}>
										<div>
											<strong>{item.label}</strong>
											<p>{item.description}</p>
											{item.hintUrl && (
												<a
													className="harness-setting-hint"
													href={item.hintUrl}
													target="_blank"
													rel="noopener noreferrer"
												>
													<ExternalLink size={11} />
													Get a key
												</a>
											)}
										</div>
										<div className="harness-setting-control">
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
												<div className="harness-setting-input-wrap">
													<input
														type={inputType}
														spellCheck={false}
														autoComplete="off"
														min={item.min}
														max={item.max}
														placeholder={isSecret ? "Paste your key here" : item.type === "url" ? "https://" : ""}
														value={(draft[item.key] as string | number | undefined) ?? ""}
														onChange={(e) => {
															const raw = e.target.value;
															if (item.type === "number") {
																const n = raw === "" ? null : Number(raw);
																void update(item.key, n);
															} else {
																void update(item.key, raw === "" ? null : raw);
															}
														}}
													/>
													{isSecret && (
														<button
															type="button"
															className="harness-setting-eye"
															onClick={() => setShowSecrets((s) => ({ ...s, [item.key]: !s[item.key] }))}
															aria-label={showSecrets[item.key] ? "Hide key" : "Show key"}
															title={showSecrets[item.key] ? "Hide key" : "Show key"}
														>
															{showSecrets[item.key] ? <EyeOff size={14} /> : <Eye size={14} />}
														</button>
													)}
												</div>
											)}
										</div>
									</div>
								);
							})}
						</section>
					))}
				</div>
			</section>
		</div>
	);
}
