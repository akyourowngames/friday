/**
 * Settings store for friday-ng.
 *
 * Settings are persisted alongside config in `~/.friday-ng/config.json` under
 * a `settings` key. Each setting has a typed schema (string, number, boolean,
 * enum) and a default value. The store handles read, write, and a generic
 * listener mechanism so the TUI can re-render when a setting changes.
 *
 * The store also supports session-scoped overrides via `withOverride()`,
 * which the agent loop can use to tweak e.g. `maxTokens` for a single
 * run without mutating the persisted value.
 */
import { EventEmitter } from "node:events";
import type { FridConfig } from "./config.ts";

export type SettingValue = string | number | boolean | string[] | null;

export type SettingType = "string" | "number" | "boolean" | "enum" | "stringList";

export interface SettingSchema {
	/** Stable key (used as the JSON property name). */
	key: string;
	/** Human-readable label for the `/settings` UI. */
	label: string;
	/** Short description shown in the UI / help text. */
	description: string;
	/** Setting type. Controls how the value is rendered + edited in `/settings`. */
	type: SettingType;
	/** Default value when the user hasn't picked one. */
	defaultValue: SettingValue;
	/** For `enum` settings: the list of valid options. */
	options?: readonly string[];
	/** For `number`: the inclusive range. */
	min?: number;
	max?: number;
}

/** The default settings schema. New settings can be registered at runtime. */
const DEFAULT_SETTINGS: Record<string, SettingSchema> = {
	showThinking: {
		key: "showThinking",
		label: "Show thinking",
		description: "Render the model's <thinking> blocks in the chat (otherwise hidden).",
		type: "boolean",
		defaultValue: false,
	},
	maxTokens: {
		key: "maxTokens",
		label: "Max output tokens",
		description: "Upper bound on tokens the model can produce per turn (0 = provider default).",
		type: "number",
		defaultValue: 0,
		min: 0,
		max: 200000,
	},
	temperature: {
		key: "temperature",
		label: "Temperature",
		description: "Sampling temperature (0 = deterministic, 1 = creative). -1 means use the model default.",
		type: "number",
		defaultValue: -1,
		min: -1,
		max: 2,
	},
	theme: {
		key: "theme",
		label: "Color theme",
		description: "Color theme for the TUI.",
		type: "enum",
		options: ["default", "mono", "solarized"],
		defaultValue: "default",
	},
	streamDebounceMs: {
		key: "streamDebounceMs",
		label: "Stream debounce (ms)",
		description: "Coalesce streaming repaints within this many ms (0 = no debounce).",
		type: "number",
		defaultValue: 0,
		min: 0,
		max: 200,
	},
	confirmToolCalls: {
		key: "confirmToolCalls",
		label: "Confirm tool calls",
		description: "Ask before executing each tool call (DANGEROUS: cancels auto-execute).",
		type: "boolean",
		defaultValue: false,
	},
	showTimestamps: {
		key: "showTimestamps",
		label: "Show timestamps",
		description: "Render timestamps next to chat entries.",
		type: "boolean",
		defaultValue: false,
	},
	compactAt: {
		key: "compactAt",
		label: "Compact at",
		description: "Auto-compact the session when the transcript reaches roughly N tokens (0 = off).",
		type: "number",
		defaultValue: 0,
		min: 0,
		max: 1000000,
	},
};

const schema = new Map<string, SettingSchema>();
for (const def of Object.values(DEFAULT_SETTINGS)) schema.set(def.key, def);

/** Register a new setting or replace an existing one. */
export function registerSetting(def: SettingSchema): void {
	schema.set(def.key, def);
}

/** Get the schema for a setting. */
export function getSettingSchema(key: string): SettingSchema | undefined {
	return schema.get(key);
}

/** List all known setting keys. */
export function listSettings(): SettingSchema[] {
	return Array.from(schema.values());
}

export interface SettingsStoreOptions {
	/** The loaded config (we read/write `settings` inside it). */
	config: FridConfig;
}

/**
 * Settings store. Wraps the persisted config with typed accessors and a
 * small EventEmitter so the TUI can listen for changes.
 */
export class SettingsStore extends EventEmitter {
	private config: FridConfig;
	private overrides = new Map<string, SettingValue>();

	constructor(options: SettingsStoreOptions) {
		super();
		this.config = options.config;
	}

	/** Read a setting, applying (in order) override → persisted → default. */
	get(key: string): SettingValue {
		if (this.overrides.has(key)) {
			return this.overrides.get(key) as SettingValue;
		}
		const persisted = (this.config as any).settings?.[key];
		if (persisted !== undefined) return persisted as SettingValue;
		const def = schema.get(key);
		return def?.defaultValue ?? null;
	}

	/** Write a setting to the persisted config. Emits a `change` event. */
	set(key: string, value: SettingValue): void {
		const def = schema.get(key);
		if (!def) {
			throw new Error(`Unknown setting: ${key}`);
		}
		if (!validateValue(def, value)) {
			throw new Error(`Invalid value for setting ${key}: ${JSON.stringify(value)}`);
		}
		const settings = { ...((this.config as any).settings ?? {}) };
		settings[key] = value;
		(this.config as any).settings = settings;
		this.emit("change", { key, value });
	}

	/** Get the persisted value (no override, no default). */
	getPersisted(key: string): SettingValue | undefined {
		return (this.config as any).settings?.[key];
	}

	/** Get a non-persisted override value. */
	getOverride(key: string): SettingValue | undefined {
		return this.overrides.get(key);
	}

	/** Set a session-scoped override (not persisted). */
	setOverride(key: string, value: SettingValue): void {
		this.overrides.set(key, value);
		this.emit("change", { key, value });
	}

	/** Remove a session-scoped override. */
	clearOverride(key: string): void {
		if (this.overrides.delete(key)) {
			this.emit("change", { key, value: this.get(key) });
		}
	}

	/** Clear all session-scoped overrides. */
	clearOverrides(): void {
		const keys = Array.from(this.overrides.keys());
		this.overrides.clear();
		for (const key of keys) this.emit("change", { key, value: this.get(key) });
	}

	/** Replace the underlying config (after a save/load round-trip). */
	replaceConfig(config: FridConfig): void {
		this.config = config;
		this.emit("change", { key: "*", value: null });
	}

	/** Snapshot of the current effective settings (override → persisted → default). */
	snapshot(): Record<string, SettingValue> {
		const out: Record<string, SettingValue> = {};
		for (const key of schema.keys()) {
			out[key] = this.get(key);
		}
		return out;
	}
}

/** Validate a value against a setting schema. */
export function validateValue(def: SettingSchema, value: SettingValue): boolean {
	if (value === null) return true;
	switch (def.type) {
		case "string":
			return typeof value === "string";
		case "number":
			if (typeof value !== "number" || !Number.isFinite(value)) return false;
			if (def.min !== undefined && value < def.min) return false;
			if (def.max !== undefined && value > def.max) return false;
			return true;
		case "boolean":
			return typeof value === "boolean";
		case "enum":
			return typeof value === "string" && (def.options ?? []).includes(value);
		case "stringList":
			return (
				Array.isArray(value) && value.every((v) => typeof v === "string")
			);
	}
}

/** Serialize the settings block for `FridConfig`. */
export function settingsToJson(store: SettingsStore): Record<string, SettingValue> {
	const out: Record<string, SettingValue> = {};
	for (const key of schema.keys()) {
		const persisted = store.getPersisted(key);
		if (persisted !== undefined) out[key] = persisted;
	}
	return out;
}
