import { loadConfig } from "@/src/config";
import { SettingsStore, settingsToJson, listSettings } from "@/src/settings";
import { findProvider, listProviders } from "@/src/providers/registry";
import { listSessions } from "@/src/sessions";
import { ChatApp } from "@/components/chat/ChatApp";

/**
 * Root page. Server-rendered so the first paint already has the user's
 * provider/model/settings and the recent-session list — no loading flash
 * before the chat becomes interactive.
 */
export const dynamic = "force-dynamic";

export default async function Page() {
	const [config, sessions] = await Promise.all([loadConfig(), listSessions()]);
	const settings = new SettingsStore({ config });
	const providerId = config.lastProvider ?? "faux";
	const provider = findProvider(providerId) ?? findProvider("faux");
	if (!provider) throw new Error("No providers registered");

	const initialData = {
		provider: provider.id,
		model: config.providers[provider.id]?.lastModel ?? provider.defaultModel,
		providers: listProviders().map((item) => ({
			id: item.id,
			name: item.name,
			defaultModel: item.defaultModel,
			requiresKey: item.requiresKey,
		})),
		settings: settingsToJson(settings),
		settingSchema: listSettings(),
		sessions: sessions.map((s) => ({
			id: s.id,
			title: s.title,
			updatedAt: s.updatedAt,
			messageCount: s.messageCount,
			provider: s.provider,
			model: s.model,
		})),
	};

	return <ChatApp initial={initialData} />;
}
