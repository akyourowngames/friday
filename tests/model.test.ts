import { describe, it, expect, beforeEach } from "vitest";
import { registerModel, getModel, listModels, clearModels } from "../src/model.ts";
import type { Model } from "../src/types.ts";

function makeModel(overrides: Partial<Model> = {}): Model {
	return {
		id: "gpt-4o",
		name: "GPT-4o",
		api: "openai",
		provider: "openai",
		baseUrl: "https://api.openai.com/v1",
		reasoning: false,
		input: ["text"],
		cost: { input: 0.03, output: 0.06, cacheRead: 0.0075, cacheWrite: 0.0075, total: 0 },
		contextWindow: 16384,
		maxTokens: 16384,
		...overrides,
	};
}

describe("model registry", () => {
	beforeEach(() => {
		clearModels();
	});

	it("should register and retrieve a model", () => {
		const model = makeModel();

		registerModel(model);
		const retrieved = getModel("gpt-4o", "openai");
		expect(retrieved).toBeDefined();
		expect(retrieved?.id).toBe("gpt-4o");
		expect(retrieved?.provider).toBe("openai");
	});

	it("should return undefined for unregistered model", () => {
		expect(getModel("nonexistent")).toBeUndefined();
	});

	it("should list all registered models", () => {
		registerModel(makeModel({ id: "model-1" }));
		registerModel(makeModel({ id: "model-2", provider: "openai" }));

		const list = listModels();
		expect(list.length).toBe(2);
		expect(list.map((m) => m.id)).toEqual(["model-1", "model-2"]);
	});

	it("should clear all models", () => {
		registerModel(makeModel());
		clearModels();
		expect(listModels().length).toBe(0);
	});

	it("should retrieve by 'provider/id' string key", () => {
		registerModel(makeModel());
		const retrieved = getModel("openai/gpt-4o");
		expect(retrieved).toBeDefined();
		expect(retrieved?.id).toBe("gpt-4o");
	});

	it("preserves registered input capabilities", () => {
		registerModel(makeModel({ input: ["text", "image"] }));
		expect(getModel("openai/gpt-4o")?.input).toEqual(["text", "image"]);
	});

	it("defaults omitted input capabilities to text", () => {
		const { input: _input, cost: _cost, ...registered } = makeModel();
		registerModel(registered);
		expect(getModel("openai/gpt-4o")?.input).toEqual(["text"]);
	});
});
