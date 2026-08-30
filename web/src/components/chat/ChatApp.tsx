"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { ProviderInfo, SessionMeta, SettingSchema } from "@/lib/types";
import { useChatSession } from "@/lib/use-chat";
import { useSessions } from "@/lib/use-sessions";
import { SessionRail } from "@/components/sidebar/SessionRail";
import { Topbar } from "@/components/chat/Topbar";
import { MessageList } from "@/components/chat/MessageList";
import { Composer } from "@/components/chat/Composer";
import { ModelPicker } from "@/components/chat/ModelPicker";
import { SettingsModal } from "@/components/settings/SettingsModal";
import { CommandPalette } from "@/components/palette/CommandPalette";
import { ThemeToggle } from "@/components/theme/ThemeToggle";
import { Menu } from "lucide-react";

export interface ChatAppInitial {
	provider: string;
	model: string;
	providers: ProviderInfo[];
	settings: Record<string, unknown>;
	settingSchema: SettingSchema[];
	sessions: SessionMeta[];
}

export function ChatApp({ initial }: { initial: ChatAppInitial }) {
	const { sessions, refresh, loadSession } = useSessions(initial.sessions);
	const [railOpen, setRailOpen] = useState(true);
	const [settingsOpen, setSettingsOpen] = useState(false);
	const [paletteOpen, setPaletteOpen] = useState(false);
	const [modelOpen, setModelOpen] = useState(false);
	const [theme, setTheme] = useState<"dark" | "light">("dark");
	const [activeSessionId, setActiveSessionId] = useState<string | null>(null);

	const chat = useChatSession({
		initialProvider: initial.provider,
		initialModel: initial.model,
		providers: initial.providers,
		settings: initial.settings,
		settingSchema: initial.settingSchema,
		sessionId: activeSessionId,
		onSessionChange: (id) => {
			setActiveSessionId(id);
			if (id) void refresh();
		},
		onSettingsChange: () => undefined,
	});

	// After every assistant turn, refresh the session list so the new
	// session (or updated timestamp on the existing one) shows up.
	useEffect(() => {
		if (!chat.isStreaming && chat.messages.length > 0) {
			void refresh();
		}
	}, [chat.isStreaming, chat.messages.length, refresh]);

	// Theme switch
	useEffect(() => {
		document.documentElement.dataset.theme = theme;
		document.documentElement.classList.toggle("dark", theme === "dark");
		document.documentElement.classList.toggle("light", theme === "light");
	}, [theme]);

	// Cmd/Ctrl+K → command palette
	useEffect(() => {
		const onKey = (e: KeyboardEvent) => {
			if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
				e.preventDefault();
				setPaletteOpen(true);
			}
		};
		window.addEventListener("keydown", onKey);
		return () => window.removeEventListener("keydown", onKey);
	}, []);

	const handleSessionPick = useCallback(
		async (id: string) => {
			const loaded = await loadSession(id);
			if (!loaded) return;
			chat.loadTranscript(loaded.meta.id, loaded.messages);
			setActiveSessionId(loaded.meta.id);
		},
		[chat, loadSession],
	);

	const handleNewSession = useCallback(() => {
		chat.resetSession();
	}, [chat]);

	const activeProviderName = useMemo(() => {
		return initial.providers.find((p) => p.id === chat.provider)?.name ?? "Connecting";
	}, [initial.providers, chat.provider]);

	// External-draft state: clicking a starter prompt on the empty state
	// pushes text into the composer.
	const [externalDraft, setExternalDraft] = useState<string | undefined>(undefined);
	const pickStarter = useCallback((text: string) => {
		setExternalDraft(text);
	}, []);

	return (
		<main className="harness-shell">
			<div className="harness-ambient harness-ambient-one" />
			<div className="harness-ambient harness-ambient-two" />

			<aside className={`harness-rail ${railOpen ? "" : "harness-rail-collapsed"}`} aria-hidden={!railOpen}>
				<SessionRail
					sessions={sessions}
					activeSessionId={activeSessionId}
					activeProviderName={activeProviderName}
					onPick={handleSessionPick}
					onNew={handleNewSession}
					onOpenSettings={() => setSettingsOpen(true)}
					onClose={() => setRailOpen(false)}
				/>
			</aside>
			{!railOpen && (
				<button
					type="button"
					className="harness-icon-button harness-rail-reopen"
					style={{ position: "fixed", top: 18, left: 14, zIndex: 11 }}
					onClick={() => setRailOpen(true)}
					aria-label="Open sidebar"
				>
					<Menu size={18} />
				</button>
			)}

			<section className="harness-workspace">
				<Topbar
					isStreaming={chat.isStreaming}
					activeSessionId={activeSessionId}
					onToggleRail={() => setRailOpen((v) => !v)}
					onOpenSettings={() => setSettingsOpen(true)}
					themeToggle={<ThemeToggle theme={theme} onChange={setTheme} />}
				/>
				<MessageList
					messages={chat.messages}
					isStreaming={chat.isStreaming}
					onToggleTool={chat.toggleTool}
					onPickStarter={pickStarter}
				/>
				<div className="harness-composer-wrap">
					<ModelPicker
						open={modelOpen}
						provider={chat.provider}
						model={chat.model}
						providers={initial.providers}
						onPick={(p, m) => {
							chat.setProvider(p);
							chat.setModel(m);
							setModelOpen(false);
						}}
						onToggle={() => setModelOpen((v) => !v)}
					/>
					<Composer
						isStreaming={chat.isStreaming}
						isListening={false}
						onSubmit={chat.submit}
						onAbort={chat.abort}
						disabled={chat.isStreaming}
						externalDraft={externalDraft}
					/>
				</div>
			</section>

			{settingsOpen && (
				<SettingsModal
					settings={chat.settings}
					schema={chat.settingSchema}
					onSave={chat.saveSettings}
					onClose={() => setSettingsOpen(false)}
				/>
			)}
			{paletteOpen && (
				<CommandPalette
					onClose={() => setPaletteOpen(false)}
					actions={{
						newSession: handleNewSession,
						openSettings: () => setSettingsOpen(true),
						toggleTheme: () => setTheme(theme === "dark" ? "light" : "dark"),
					}}
				/>
			)}
		</main>
	);
}
