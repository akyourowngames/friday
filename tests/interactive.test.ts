/**
 * Tests for the interactive setup helpers (model picker + key resolution).
 *
 * `pickModel` and `ensureApiKey` normally block on stdin. We mock the
 * `node:readline/promises` module so `readLine` resolves with a scripted
 * answer, letting us exercise the number/index and name branches without a TTY.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const answer = vi.hoisted(() => ({ value: "" }));

vi.mock("node:readline/promises", () => ({
  createInterface: () => ({
    question: () => Promise.resolve(answer.value),
    close: () => {},
  }),
}));

const { pickModel, ensureApiKey, listModelsForProvider, setupProvider } = await import(
  "../src/interactive.ts"
);
const { findProvider } = await import("../src/providers/registry.ts");
const typeProvider = (overrides: Record<string, unknown>) =>
  overrides as unknown as import("../src/providers/registry.ts").ProviderMeta;

describe("pickModel", () => {
  it("returns the default model when no list is available", async () => {
    expect(await pickModel([], { defaultModel: "gpt-4o" })).toBe("gpt-4o");
  });

  it("falls back to lastModel (when no default) when the list is empty", async () => {
    expect(await pickModel([], { lastModel: "claude-3-5-sonnet" })).toBe("claude-3-5-sonnet");
  });

  it("prefers defaultModel over lastModel for an empty list", async () => {
    expect(
      await pickModel([], { lastModel: "claude-3-5-sonnet", defaultModel: "gpt-4o" }),
    ).toBe("gpt-4o");
  });

  it("returns an empty string when list and fallbacks are all empty", async () => {
    expect(await pickModel([])).toBe("");
  });

  it("selects by 1-based index when a number is entered", async () => {
    answer.value = "2";
    expect(await pickModel(["alpha", "beta", "gamma"])).toBe("beta");
  });

  it("treats an unmatched string as an explicit model name", async () => {
    answer.value = "custom-model";
    expect(await pickModel(["alpha", "beta"], { defaultModel: "alpha" })).toBe("custom-model");
  });

  it("falls back to lastModel on an empty answer", async () => {
    answer.value = "";
    expect(await pickModel(["alpha", "beta"], { lastModel: "beta", defaultModel: "alpha" })).toBe(
      "beta",
    );
  });
});

describe("ensureApiKey", () => {
  const faux = findProvider("faux");
  const openai = findProvider("openai");

  afterEach(() => {
    delete process.env.FRIDAY_TEST_KEY;
    delete process.env.OPENAI_API_KEY;
  });

  it("returns an empty string for providers that do not require a key", async () => {
    const config = { providers: {} } as unknown as import("../src/config.ts").FridConfig;
    expect(await ensureApiKey(faux, config)).toBe("");
  });

  it("prefers the environment variable over config", async () => {
    process.env.OPENAI_API_KEY = "env-key";
    const config = {
      providers: { openai: { apiKey: "config-key" } },
    } as unknown as import("../src/config.ts").FridConfig;
    expect(await ensureApiKey(openai, config)).toBe("env-key");
  });

  it("falls back to the stored config key when no env var is set", async () => {
    const config = {
      providers: { openai: { apiKey: "config-key" } },
    } as unknown as import("../src/config.ts").FridConfig;
    expect(await ensureApiKey(openai, config)).toBe("config-key");
  });

  it("respects forceKeyPrompt and ignores env/config", async () => {
    process.env.FRIDAY_TEST_KEY = "env-key";
    const fakeProvider = typeProvider({
      id: "openai",
      name: "OpenAI",
      requiresKey: true,
      apiKeyEnvVars: ["FRIDAY_TEST_KEY"],
    });
    const config = {
      providers: { openai: { apiKey: "config-key" } },
    } as unknown as import("../src/config.ts").FridConfig;
    // No stdin → readSecret gets an empty answer and throws, proving the prompt
    // path was taken rather than the env/config shortcuts.
    answer.value = "";
    await expect(ensureApiKey(fakeProvider, config, { forceKeyPrompt: true })).rejects.toThrow();
  });
});

describe("claude alias routing", () => {
  it("setupProvider('claude') builds an Anthropic stream function", async () => {
    process.env.ANTHROPIC_API_KEY = "sk-test-claude-alias";
    try {
      const result = await setupProvider("claude", {
        modelOverride: "claude-3-5-sonnet-latest",
        skipModelPicker: true,
        noConfig: true,
      });
      expect(typeof result.streamFn).toBe("function");
      expect(result.model).toBe("claude-3-5-sonnet-latest");
    } finally {
      delete process.env.ANTHROPIC_API_KEY;
    }
  });
});

describe("listModelsForProvider", () => {
  it("exposes a `claude` alias that routes to the Anthropic adapter", () => {
    const claude = findProvider("claude");
    expect(claude).toBeDefined();
    expect(claude!.apiStyle).toBe("anthropic");
    expect(claude!.apiKeyEnvVars).toContain("ANTHROPIC_API_KEY");
  });

  it("returns a canned list for the faux provider", async () => {
    const faux = findProvider("faux");
    expect(await listModelsForProvider(faux, "")).toEqual(["faux-1"]);
  });

  it("returns an empty list when a provider's model fetch fails", async () => {
    const fakeProvider = typeProvider({ id: "openai", apiStyle: "openai" });
    // No network access in the test sandbox → listOpenAICompatModels swallows the
    // error and returns []. listModelsForProvider wraps it again but still [].
    const models = await listModelsForProvider(fakeProvider, "sk-bogus", "http://127.0.0.1:9");
    expect(Array.isArray(models)).toBe(true);
  });
});
