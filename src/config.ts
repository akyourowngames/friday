/**
 * Config persistence for friday-ng.
 *
 * Stores API keys and last-selected model per provider in `~/.friday-ng/config.json`.
 * File mode is 0600 on Unix systems; the parent directory is 0700.
 *
 * Resolution order for a provider's API key:
 * 1. Environment variable (e.g. OPENAI_API_KEY)
 * 2. Config file (~/.friday-ng/config.json)
 * 3. Interactive prompt (handled by src/interactive.ts)
 */
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";

/** Per-provider config. */
export interface ProviderConfig {
	apiKey?: string;
	baseUrl?: string;
	lastModel?: string;
	/** Optional bearer token (some gateways authenticate via Authorization:
	 *  Bearer instead of / in addition to x-api-key). */
	authToken?: string;
}

/** Setting value type — re-exported from settings.ts. */
export type { SettingValue } from "./settings.ts";

/** Settings block inside FridConfig. Loose `Record` so unknown keys don't
 *  trip up older builds. */
export type FridSettings = Record<string, unknown>;

/** Root config schema. */
export interface FridConfig {
	providers: Record<string, ProviderConfig>;
	lastProvider?: string;
	/** User-configurable settings. Persisted alongside the rest of the config. */
	settings?: FridSettings;
	/** Saved list of recent session ids (most recent first). */
	recentSessions?: string[];
	/** Last session id the user resumed from, so the TUI can pick it up
	 *  automatically on the next launch. */
	lastSessionId?: string;
}

const DEFAULT_CONFIG: FridConfig = {
	providers: {},
};

function getConfigDir(): string {
	return process.env.FRIDAY_NG_CONFIG_DIR ?? path.join(os.homedir(), ".friday-ng");
}

function getConfigPath(): string {
	return path.join(getConfigDir(), "config.json");
}

/** Read config from disk. Returns default config if missing or invalid. */
export async function loadConfig(): Promise<FridConfig> {
	try {
		const raw = await fs.readFile(getConfigPath(), "utf8");
		const parsed = JSON.parse(raw) as FridConfig;
		// Validate structure
		if (typeof parsed !== "object" || parsed === null) return { ...DEFAULT_CONFIG };
		return {
			providers: parsed.providers && typeof parsed.providers === "object" ? parsed.providers : {},
			lastProvider: typeof parsed.lastProvider === "string" ? parsed.lastProvider : undefined,
			settings:
				parsed.settings && typeof parsed.settings === "object" && !Array.isArray(parsed.settings)
					? (parsed.settings as FridSettings)
					: undefined,
			recentSessions: Array.isArray(parsed.recentSessions)
				? parsed.recentSessions.filter((s): s is string => typeof s === "string")
				: undefined,
			lastSessionId: typeof parsed.lastSessionId === "string" ? parsed.lastSessionId : undefined,
		};
	} catch (err: any) {
		// File doesn't exist or is invalid → return default
		if (err?.code === "ENOENT") return { ...DEFAULT_CONFIG, providers: {} };
		return { ...DEFAULT_CONFIG, providers: {} };
	}
}

/** Atomic write: write to tmp file, then rename. Creates dir if missing. */
export async function saveConfig(config: FridConfig): Promise<void> {
	const dir = getConfigDir();
	const target = getConfigPath();
	const tmp = path.join(dir, `.config.${process.pid}.tmp`);

	await fs.mkdir(dir, { recursive: true, mode: 0o700 });
	await fs.writeFile(tmp, JSON.stringify(config, null, 2), { mode: 0o600 });
	await fs.rename(tmp, target);

	// Ensure file mode 0600 (rename may copy with default mode on some FS)
	try {
		await fs.chmod(target, 0o600);
	} catch {
		// chmod is not available on Windows in some contexts; ignore
	}
}

/** Get the config file path (for display). */
export function getConfigFilePath(): string {
	return getConfigPath();
}

/** Get the API key for a provider from env or config. Returns undefined if neither has it. */
export function getApiKey(providerId: string, envVar: string, config: FridConfig): string | undefined {
	const fromEnv = process.env[envVar];
	if (fromEnv) return fromEnv;
	return config.providers[providerId]?.apiKey;
}

/** Get the base URL override for a provider (from env or config). */
export function getBaseUrl(envVar: string | undefined, config: FridConfig, providerId: string): string | undefined {
	if (envVar) {
		const fromEnv = process.env[envVar];
		if (fromEnv) return fromEnv;
	}
	return config.providers[providerId]?.baseUrl;
}

/** Get the last-selected model for a provider. */
export function getLastModel(providerId: string, config: FridConfig): string | undefined {
	return config.providers[providerId]?.lastModel;
}

/** Update the config with a new API key and return the updated config. */
export function withApiKey(config: FridConfig, providerId: string, apiKey: string): FridConfig {
	return {
		...config,
		providers: {
			...config.providers,
			[providerId]: {
				...config.providers[providerId],
				apiKey,
			},
		},
	};
}

/** Update the config with a new base URL and return the updated config. */
export function withBaseUrl(config: FridConfig, providerId: string, baseUrl: string): FridConfig {
	return {
		...config,
		providers: {
			...config.providers,
			[providerId]: {
				...config.providers[providerId],
				baseUrl,
			},
		},
	};
}

/** Update the config with a new auth token and return the updated config. */
export function withAuthToken(config: FridConfig, providerId: string, authToken: string): FridConfig {
	return {
		...config,
		providers: {
			...config.providers,
			[providerId]: {
				...config.providers[providerId],
				authToken,
			},
		},
	};
}

/** Update the config with a new last-selected model and return the updated config. */
export function withLastModel(config: FridConfig, providerId: string, model: string): FridConfig {
	return {
		...config,
		providers: {
			...config.providers,
			[providerId]: {
				...config.providers[providerId],
				lastModel: model,
			},
		},
	};
}

/** Update the config's lastProvider. */
export function withLastProvider(config: FridConfig, providerId: string): FridConfig {
	return { ...config, lastProvider: providerId };
}

/** Reset (clear) the config. */
export async function resetConfig(): Promise<void> {
	try {
		await fs.unlink(getConfigPath());
	} catch {
		// ignore if doesn't exist
	}
}

/** Update the config with a new `settings` block and return the updated config. */
export function withSettings(config: FridConfig, settings: FridSettings): FridConfig {
	return { ...config, settings: { ...config.settings, ...settings } };
}

/** Update the config with a recent-sessions list and return the updated config. */
export function withRecentSessions(
	config: FridConfig,
	recent: string[],
	limit = 10,
): FridConfig {
	return { ...config, recentSessions: recent.slice(0, limit) };
}

/** Update the config with a last-session id and return the updated config. */
export function withLastSessionId(config: FridConfig, id: string | undefined): FridConfig {
	if (id === undefined) {
		const { lastSessionId: _drop, ...rest } = config;
		return rest;
	}
	return { ...config, lastSessionId: id };
}

/** Add (or move-to-front) a session id in the recent-sessions list. */
export function bumpRecentSession(
	config: FridConfig,
	id: string,
	limit = 10,
): FridConfig {
	const current = config.recentSessions ?? [];
	const next = [id, ...current.filter((s) => s !== id)].slice(0, limit);
	return withRecentSessions(config, next, limit);
}
