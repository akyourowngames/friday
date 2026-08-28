import { describe, it, expect, beforeEach, afterEach } from "vitest";
import * as os from "node:os";
import * as path from "node:path";
import { promises as fs } from "node:fs";
import {
	loadConfig,
	saveConfig,
	getConfigFilePath,
	withApiKey,
	withLastModel,
	withLastProvider,
	withBaseUrl,
	resetConfig,
} from "../src/config.ts";

describe("config", () => {
	const tmpDir = path.join(os.tmpdir(), `friday-ng-test-${Date.now()}-${Math.random()}`);

	beforeEach(async () => {
		await fs.mkdir(tmpDir, { recursive: true });
		process.env.FRIDAY_NG_CONFIG_DIR = tmpDir;
	});

	afterEach(async () => {
		delete process.env.FRIDAY_NG_CONFIG_DIR;
		await fs.rm(tmpDir, { recursive: true, force: true });
	});

	it("loadConfig returns default when file missing", async () => {
		const config = await loadConfig();
		expect(config).toBeDefined();
		expect(config.providers).toEqual({});
	});

	it("saveConfig then loadConfig round-trips", async () => {
		const original = {
			providers: {
				openai: { apiKey: "sk-test123", lastModel: "gpt-4o" },
			},
			lastProvider: "openai",
		};
		await saveConfig(original);
		const loaded = await loadConfig();
		expect(loaded.providers.openai?.apiKey).toBe("sk-test123");
		expect(loaded.providers.openai?.lastModel).toBe("gpt-4o");
		expect(loaded.lastProvider).toBe("openai");
	});

	it("loadConfig handles corrupted JSON gracefully", async () => {
		const configPath = getConfigFilePath();
		await fs.mkdir(path.dirname(configPath), { recursive: true });
		await fs.writeFile(configPath, "not valid json {{{");
		const config = await loadConfig();
		expect(config.providers).toEqual({});
	});

	it("getConfigFilePath reflects env override", () => {
		const path = getConfigFilePath();
		expect(path).toContain("friday-ng-test-");
	});

	it("withApiKey creates a new config with key set", () => {
		const config = withApiKey({ providers: {} }, "openai", "sk-key1");
		expect(config.providers.openai?.apiKey).toBe("sk-key1");
	});

	it("withApiKey preserves other provider config", () => {
		const initial = { providers: { openai: { lastModel: "gpt-4o" } } };
		const next = withApiKey(initial, "openai", "sk-key1");
		expect(next.providers.openai?.lastModel).toBe("gpt-4o");
		expect(next.providers.openai?.apiKey).toBe("sk-key1");
	});

	it("withLastModel sets model", () => {
		const config = withLastModel({ providers: {} }, "openai", "gpt-4-turbo");
		expect(config.providers.openai?.lastModel).toBe("gpt-4-turbo");
	});

	it("withLastProvider sets lastProvider", () => {
		const config = withLastProvider({ providers: {} }, "anthropic");
		expect(config.lastProvider).toBe("anthropic");
	});

	it("withBaseUrl sets baseUrl", () => {
		const config = withBaseUrl({ providers: {} }, "openai", "https://my-proxy.com/v1");
		expect(config.providers.openai?.baseUrl).toBe("https://my-proxy.com/v1");
	});

	it("resetConfig removes the file", async () => {
		await saveConfig({ providers: { openai: { apiKey: "sk-test" } } });
		await resetConfig();
		const config = await loadConfig();
		expect(config.providers).toEqual({});
	});

	it("saveConfig creates the directory if missing", async () => {
		const nestedDir = path.join(tmpDir, "nested", "deep");
		process.env.FRIDAY_NG_CONFIG_DIR = nestedDir;
		await saveConfig({ providers: { openai: { apiKey: "sk-x" } } });
		const exists = await fs.stat(path.join(nestedDir, "config.json")).then(() => true).catch(() => false);
		expect(exists).toBe(true);
	});
});
