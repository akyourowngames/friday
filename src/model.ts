import type { Api, Model, ProviderId } from "./types.ts";

/** A simple model registry for friday-ng. Providers register their models here
 *  at startup; callers look them up via getModel(). */

const EMPTY_COST = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 };

interface RegisteredModel {
	id: string;
	name: string;
	api: Api;
	provider: ProviderId;
	baseUrl: string;
	reasoning: boolean;
	input?: ("text" | "image")[];
	contextWindow: number;
	maxTokens: number;
}

const registry = new Map<string, RegisteredModel>();

/** Register a model. Key: "provider/modelId". */
export function registerModel(model: RegisteredModel): void {
	registry.set(`${model.provider}/${model.id}`, model);
}

/** Look up a model by provider + id, or by "provider/modelId" string.
 *  Returns undefined if not found. */
export function getModel<TApi extends Api = Api>(
	lookup: string,
	provider?: ProviderId,
): Model<TApi> | undefined {
	const key = provider ? `${provider}/${lookup}` : lookup;
	const found = registry.get(key);
	if (!found) {
		return undefined;
	}
	return {
		id: found.id,
		name: found.name,
		api: found.api as TApi,
		provider: found.provider,
		baseUrl: found.baseUrl,
		reasoning: found.reasoning,
		input: found.input ?? ["text"],
		cost: EMPTY_COST,
		contextWindow: found.contextWindow,
		maxTokens: found.maxTokens,
	};
}

export function listModels(): RegisteredModel[] {
	return Array.from(registry.values());
}

export function clearModels(): void {
	registry.clear();
}
