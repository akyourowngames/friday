import { NextResponse } from "next/server";
import { loadConfig } from "@/src/config";
import { findProvider, listProviders } from "@/src/providers/registry";
import { SettingsStore, listSettings, settingsToJson } from "@/src/settings";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
	const config = await loadConfig();
	const providerId = config.lastProvider ?? "faux";
	const provider = findProvider(providerId) ?? findProvider("faux");
	if (!provider) {
		return NextResponse.json({ error: "No providers registered" }, { status: 500 });
	}
	const settings = new SettingsStore({ config });
	return NextResponse.json({
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
	});
}
