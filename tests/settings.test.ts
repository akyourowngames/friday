import { describe, it, expect, beforeEach } from "vitest";
import { SettingsStore, listSettings, registerSetting, getSettingSchema, validateValue } from "../src/settings.ts";
import type { FridConfig } from "../src/config.ts";

describe("settings store", () => {
	let config: FridConfig;
	let store: SettingsStore;

	beforeEach(() => {
		config = { providers: {} };
		store = new SettingsStore({ config });
	});

	it("returns defaults when nothing is set", () => {
		expect(store.get("showThinking")).toBe(false);
		expect(store.get("theme")).toBe("default");
	});

	it("set() writes through to the underlying config", () => {
		store.set("showThinking", true);
		expect(store.get("showThinking")).toBe(true);
		expect((config as any).settings?.showThinking).toBe(true);
	});

	it("set() emits a change event", () => {
		const events: any[] = [];
		store.on("change", (e) => events.push(e));
		store.set("theme", "solarized");
		expect(events).toHaveLength(1);
		expect(events[0].key).toBe("theme");
		expect(events[0].value).toBe("solarized");
	});

	it("set() rejects unknown keys", () => {
		expect(() => store.set("doesNotExist", "x" as any)).toThrow();
	});

	it("set() rejects values outside the schema range", () => {
		expect(() => store.set("temperature", 5 as any)).toThrow();
	});

	it("set() rejects invalid enum values", () => {
		expect(() => store.set("theme", "neon-pink" as any)).toThrow();
	});

	it("setOverride takes precedence over persisted", () => {
		store.set("showThinking", true);
		store.setOverride("showThinking", false);
		expect(store.get("showThinking")).toBe(false);
		store.clearOverride("showThinking");
		expect(store.get("showThinking")).toBe(true);
	});

	it("snapshot returns every registered setting", () => {
		const snap = store.snapshot();
		for (const def of listSettings()) {
			expect(snap[def.key]).toBeDefined();
		}
	});

	it("registerSetting adds a new setting", () => {
		registerSetting({ key: "test", label: "Test", description: "x", type: "string", defaultValue: "hi" });
		const def = getSettingSchema("test");
		expect(def?.defaultValue).toBe("hi");
	});

	it("validateValue handles every type", () => {
		expect(validateValue({ key: "s", label: "l", description: "d", type: "string", defaultValue: "" }, "x")).toBe(true);
		expect(validateValue({ key: "s", label: "l", description: "d", type: "string", defaultValue: "" }, 1 as any)).toBe(false);
		expect(validateValue({ key: "s", label: "l", description: "d", type: "number", defaultValue: 0 }, 3.14)).toBe(true);
		expect(validateValue({ key: "s", label: "l", description: "d", type: "number", defaultValue: 0 }, "x" as any)).toBe(false);
		expect(validateValue({ key: "s", label: "l", description: "d", type: "boolean", defaultValue: false }, true)).toBe(true);
		expect(validateValue({ key: "s", label: "l", description: "d", type: "boolean", defaultValue: false }, "yes" as any)).toBe(false);
		expect(validateValue({ key: "s", label: "l", description: "d", type: "enum", defaultValue: "a", options: ["a", "b"] }, "a")).toBe(true);
		expect(validateValue({ key: "s", label: "l", description: "d", type: "enum", defaultValue: "a", options: ["a", "b"] }, "c" as any)).toBe(false);
		expect(validateValue({ key: "s", label: "l", description: "d", type: "stringList", defaultValue: [] }, ["a"])).toBe(true);
		expect(validateValue({ key: "s", label: "l", description: "d", type: "stringList", defaultValue: [] }, [1] as any)).toBe(false);
	});

	it("respects persisted values when overrides are not set", () => {
		const persisted: FridConfig = { providers: {}, settings: { showThinking: true } };
		const s = new SettingsStore({ config: persisted });
		expect(s.get("showThinking")).toBe(true);
	});
});
