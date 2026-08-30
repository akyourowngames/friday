import { NextResponse } from "next/server";
import { loadConfig, saveConfig, withSettings } from "@/src/config";
import { SettingsStore, settingsToJson } from "@/src/settings";
import type { SettingValue } from "@/src/settings";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

interface Body {
	settings?: Record<string, SettingValue>;
}

export async function POST(req: Request) {
	const body = (await req.json().catch(() => ({}))) as Body;
	const config = await loadConfig();
	const store = new SettingsStore({ config });
	for (const [key, value] of Object.entries(body.settings ?? {})) {
		store.set(key, value as SettingValue);
	}
	await saveConfig(withSettings(config, settingsToJson(store)));
	return NextResponse.json({ settings: settingsToJson(store) });
}
