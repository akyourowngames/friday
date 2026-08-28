/**
 * Ollama provider (local).
 *
 * Uses the `openai` SDK pointed at Ollama's OpenAI-compatible endpoint
 * (http://localhost:11434/v1). No API key required.
 */
import type { Api, Model, StreamFn, StreamOptions } from "../types.ts";
import { createOpenAICompatStreamFn, listOpenAICompatModels } from "./openai-compat.ts";

export interface OllamaConfig {
	model?: string;
	baseUrl?: string;
}

const OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1";

/** Create a StreamFn for Ollama. */
export function createOllamaStreamFn(config: OllamaConfig = {}): StreamFn {
	return createOpenAICompatStreamFn({
		model: config.model ?? "llama3.2",
		apiKey: "ollama", // Ollama ignores the key but the openai SDK requires one
		baseUrl: config.baseUrl ?? OLLAMA_DEFAULT_BASE_URL,
	});
}

/** List installed Ollama models. Returns [] if Ollama isn't running. */
export async function listOllamaModels(config: OllamaConfig = {}): Promise<string[]> {
	return listOpenAICompatModels({
		apiKey: "ollama",
		baseUrl: config.baseUrl ?? OLLAMA_DEFAULT_BASE_URL,
	});
}

/** Check if Ollama is reachable at the given baseUrl. */
export async function isOllamaRunning(baseUrl: string = OLLAMA_DEFAULT_BASE_URL): Promise<boolean> {
	try {
		const response = await fetch(`${baseUrl.replace(/\/v1\/?$/, "")}/api/tags`, {
			signal: AbortSignal.timeout(2000),
		});
		return response.ok;
	} catch {
		return false;
	}
}
