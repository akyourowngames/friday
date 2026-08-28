import { describe, it, expect } from "vitest";
import {
	getProvider,
	findProvider,
	listProviders,
	resolveApiKey,
	resolveBaseUrl,
} from "../../src/providers/registry.ts";

describe("provider registry", () => {
	it("lists all providers", () => {
		const providers = listProviders();
		expect(providers.length).toBeGreaterThan(5);
		const ids = providers.map((p) => p.id);
		expect(ids).toContain("faux");
		expect(ids).toContain("openai");
		expect(ids).toContain("anthropic");
		expect(ids).toContain("google");
		expect(ids).toContain("ollama");
	});

	it("getProvider returns a provider by id", () => {
		const openai = getProvider("openai");
		expect(openai.id).toBe("openai");
		expect(openai.name).toBe("OpenAI");
		expect(openai.requiresKey).toBe(true);
	});

	it("getProvider throws for unknown id", () => {
		expect(() => getProvider("nonexistent")).toThrowError(/Unknown provider/);
	});

	it("findProvider returns undefined for unknown id", () => {
		expect(findProvider("nonexistent")).toBeUndefined();
	});

	it("openai provider has correct config", () => {
		const openai = getProvider("openai");
		expect(openai.apiKeyEnvVars).toContain("OPENAI_API_KEY");
		expect(openai.defaultBaseUrl).toBe("https://api.openai.com/v1");
		expect(openai.defaultModel).toBe("gpt-4o-mini");
		expect(openai.apiStyle).toBe("openai");
	});

	it("groq provider uses verified OpenAI-compatible baseUrl", () => {
		const groq = getProvider("groq");
		expect(groq.defaultBaseUrl).toBe("https://api.groq.com/openai/v1");
		expect(groq.apiStyle).toBe("openai");
	});

	it("openrouter provider uses verified OpenAI-compatible baseUrl", () => {
		const openrouter = getProvider("openrouter");
		expect(openrouter.defaultBaseUrl).toBe("https://openrouter.ai/api/v1");
	});

	it("kilo provider uses the verified Kilo gateway baseUrl", () => {
		const kilo = getProvider("kilo");
		expect(kilo.defaultBaseUrl).toBe("https://api.kilo.ai/api/gateway");
		expect(kilo.apiKeyEnvVars).toContain("KILO_API_KEY");
	});

	it("ollama provider does not require a key", () => {
		const ollama = getProvider("ollama");
		expect(ollama.requiresKey).toBe(false);
		expect(ollama.apiKeyEnvVars).toEqual([]);
		expect(ollama.defaultBaseUrl).toBe("http://localhost:11434/v1");
	});

	it("anthropic provider uses claude-3.5-sonnet by default", () => {
		const anthropic = getProvider("anthropic");
		expect(anthropic.apiStyle).toBe("anthropic");
		expect(anthropic.defaultModel).toContain("claude");
	});

	it("google provider uses gemini by default", () => {
		const google = getProvider("google");
		expect(google.apiStyle).toBe("gemini");
		expect(google.defaultModel).toContain("gemini");
	});

	it("resolveApiKey returns env var value if set", () => {
		process.env.OPENAI_API_KEY = "sk-from-env";
		const value = resolveApiKey("openai");
		expect(value).toBe("sk-from-env");
		delete process.env.OPENAI_API_KEY;
	});

	it("resolveApiKey returns undefined when no env var", () => {
		delete process.env.OPENAI_API_KEY;
		expect(resolveApiKey("openai")).toBeUndefined();
	});

	it("resolveBaseUrl returns default when no env override", () => {
		delete process.env.OPENAI_BASE_URL;
		expect(resolveBaseUrl("openai")).toBe("https://api.openai.com/v1");
	});

	it("resolveBaseUrl returns env override when set", () => {
		process.env.OPENAI_BASE_URL = "https://my-proxy.com/v1";
		expect(resolveBaseUrl("openai")).toBe("https://my-proxy.com/v1");
		delete process.env.OPENAI_BASE_URL;
	});
});
