/**
 * Interactive setup flow for friday-ng.
 *
 * - ensureApiKey: env var → config file → prompt with hidden input
 * - pickModel: numbered list, last-used highlighted
 * - setupProvider: orchestrates the above and returns stream function + model
 */
import * as readline from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import {
	type FridConfig,
	loadConfig,
	saveConfig,
	withApiKey,
	withAuthToken,
	withBaseUrl,
	withLastModel,
	withLastProvider,
} from "./config.ts";
import { findProvider, listProviders, resolveApiKey, type ProviderMeta } from "./providers/registry.ts";
import { createOpenAICompatStreamFn, listOpenAICompatModels } from "./providers/openai-compat.ts";
import { createAnthropicStreamFn, listAnthropicModels } from "./providers/anthropic.ts";
import { createGoogleStreamFn, listGoogleModels } from "./providers/google.ts";
import { createOllamaStreamFn, listOllamaModels, isOllamaRunning } from "./providers/ollama.ts";
import { createFauxStreamFn, registerFauxProvider, fauxText } from "./provider-faux.ts";
import { createFreeccStreamFn, listFreeccModels } from "./providers/freecc.ts";
import type { StreamFn } from "./types.ts";

export interface SetupOptions {
	/** Force a key prompt even if env or config has one. */
	forceKeyPrompt?: boolean;
	apiKeyOverride?: string;
	/** Skip the model picker (use default or last). */
	skipModelPicker?: boolean;
	/** Skip saving to config. */
	noConfig?: boolean;
}

export interface SetupResult {
	streamFn: StreamFn;
	model: string;
	apiKey?: string;
	/** Base URL used (resolved env → config → provider default). */
	baseUrl?: string;
	/** Bearer auth token, if the provider/gateway uses one. */
	authToken?: string;
}

/** Read a secret from stdin with hidden characters. */
export async function readSecret(prompt: string): Promise<string> {
	// If we're in a TTY with raw mode available, hide keystrokes by
	// intercepting each character.  Otherwise fall back to the readline
	// mock-friendly path (used in tests and non-TTY environments).
	if (typeof input.setRawMode === "function" && input.isTTY) {
		const wasRaw = input.isRaw;
		const chunks: string[] = [];
		try {
			output.write(prompt);
			input.setRawMode(true);
			input.resume();
			const answer = await new Promise<string>((resolve) => {
				const onData = (chunk: Buffer) => {
					const s = chunk.toString();
					for (const ch of s) {
						if (ch === "\r" || ch === "\n") {
							input.removeListener("data", onData);
							output.write("\n");
							resolve(chunks.join(""));
							return;
						}
						if (ch === "\x7f" || ch === "\b") {
							if (chunks.length > 0) {
								chunks.pop();
								output.write("\b \b"); // erase last asterisk
							}
						} else if (ch >= " ") {
							chunks.push(ch);
							output.write("*"); // show asterisk instead of the char
						}
				}
			};
			input.on("data", onData);
			});
			return answer.trim();
		} finally {
			if (wasRaw !== undefined) input.setRawMode(wasRaw);
		}
	}
	// Fallback: readline-based prompt (mock-friendly, non-TTY).
	const rl = readline.createInterface({ input, output });
	try {
		output.write(prompt);
		const answer = await rl.question("");
		output.write("\n");
		return answer.trim();
	} finally {
		rl.close();
	}
}

/** Read a regular line from stdin. */
export async function readLine(prompt: string): Promise<string> {
	const rl = readline.createInterface({ input, output });
	try {
		const answer = await rl.question(prompt);
		return answer.trim();
	} finally {
		rl.close();
	}
}

/** Get the API key for a provider: env var → config → prompt. */
export async function ensureApiKey(
	provider: ProviderMeta,
	config: FridConfig,
	options: SetupOptions = {},
): Promise<string> {
	if (!provider.requiresKey) {
		return ""; // local providers
	}
	if (options.apiKeyOverride) return options.apiKeyOverride;

	// 1. Env var
	if (!options.forceKeyPrompt) {
		for (const envVar of provider.apiKeyEnvVars) {
			const v = process.env[envVar];
			if (v) return v;
		}
	}

	// 2. Config file
	if (!options.forceKeyPrompt) {
		const stored = config.providers[provider.id]?.apiKey;
		if (stored) return stored;
	}

	// 3. Prompt
	if (provider.keyUrl) {
		output.write(`\nNo API key configured for "${provider.name}".\n`);
		output.write(`Get one at: ${provider.keyUrl}\n\n`);
	}
	const envHint = provider.apiKeyEnvVars[0]
		? `  (or set ${provider.apiKeyEnvVars[0]} in your environment)\n`
		: "";
	const key = await readSecret(`Paste your ${provider.name} API key: ${envHint}\n> `);

	if (!key) {
		throw new Error(`No API key provided for ${provider.name}.`);
	}

	// 4. Save to config
	if (!options.noConfig) {
		const updated = withApiKey(config, provider.id, key);
		await saveConfig(updated);
		output.write(`✓ Saved to ${getConfigFilePath()}\n\n`);
	}

	return key;
}

/** Pick a model from a numbered list. Returns the model id. */
export async function pickModel(
	models: string[],
	options: { lastModel?: string; defaultModel?: string } = {},
): Promise<string> {
	if (models.length === 0) {
		// No list available — just use default
		return options.defaultModel ?? options.lastModel ?? "";
	}

	// Show list
	output.write("\nAvailable models:\n");
	const maxNum = models.length.toString().length;
	for (let i = 0; i < models.length; i++) {
		const isLast = models[i] === options.lastModel;
		const isDefault = models[i] === options.defaultModel;
		let marker = "";
		if (isLast && isDefault) marker = "  ← last used & default";
		else if (isLast) marker = "  ← last used";
		else if (isDefault) marker = "  ← default";
		output.write(`  ${(i + 1).toString().padStart(maxNum)}. ${models[i]}${marker}\n`);
	}
	output.write("\n");

	const answer = await readLine(`Pick a model (1-${models.length}) or type a name: `);

	// Empty → use last/default
	if (!answer) {
		return options.lastModel ?? options.defaultModel ?? models[0]!;
	}

	// Number → index
	const n = parseInt(answer, 10);
	if (!isNaN(n) && n >= 1 && n <= models.length) {
		return models[n - 1]!;
	}

	// Otherwise treat as a name (exact match or fallback)
	return answer;
}

/** List models for a provider, handling each API style. */
export async function listModelsForProvider(
	provider: ProviderMeta,
	apiKey: string,
	baseUrl?: string,
): Promise<string[]> {
	// For gateways that authenticate via a bearer token, prefer the saved
	// config token over (and falling back to) the env var. `npm start` runs with
	// no env vars, so the config is the source of truth.
	const config = await loadConfig();
	const authToken = process.env.ANTHROPIC_AUTH_TOKEN ?? config.providers[provider.id]?.authToken;
	try {
		switch (provider.apiStyle) {
			case "openai":
				return await listOpenAICompatModels({ apiKey, baseUrl });
			case "ollama":
				return await listOllamaModels({ baseUrl });
			case "anthropic":
				return await listAnthropicModels({ apiKey, baseUrl, authToken });
			case "freecc":
				return await listFreeccModels({ apiKey, baseUrl });
			case "gemini":
				return await listGoogleModels({ apiKey, baseUrl });
			case "faux":
				return ["faux-1"];
			default:
				return [];
		}
	} catch (err) {
		return [];
	}
}

/** End-to-end setup: ensure key, pick model, return StreamFn. */
export async function setupProvider(
	providerId: string,
	options: SetupOptions & { modelOverride?: string } = {},
): Promise<SetupResult> {
	const provider = findProvider(providerId);
	if (!provider) {
		throw new Error(
			`Unknown provider: "${providerId}". Available: ${listProviders().map((p) => p.id).join(", ")}`,
		);
	}

	const config = await loadConfig();

	// Ensure API key
	const apiKey = await ensureApiKey(provider, config, options);

	// Resolve baseUrl
	const baseUrl = provider.baseUrlEnvVar
		? process.env[provider.baseUrlEnvVar] ?? config.providers[provider.id]?.baseUrl ?? provider.defaultBaseUrl
		: config.providers[provider.id]?.baseUrl ?? provider.defaultBaseUrl;

	// Resolve auth token (some gateways authenticate via `Authorization: Bearer`
	// instead of / in addition to `x-api-key`). Prefer env, then saved config.
	const authToken =
		provider.apiStyle === "anthropic"
			? process.env.ANTHROPIC_AUTH_TOKEN ?? config.providers[provider.id]?.authToken
			: undefined;

	// Resolve model
	let model = options.modelOverride ?? config.providers[provider.id]?.lastModel ?? provider.defaultModel;

	// Skip the interactive picker when: the provider points at a custom gateway
	// (non-canonical base URL — e.g. a local proxy that exposes hundreds of
	// models), or when the fetched list is too large to be usefully picked from.
	const isCustomGateway =
		!!baseUrl && !!provider.defaultBaseUrl && baseUrl !== provider.defaultBaseUrl;
	const MAX_PICKABLE_MODELS = 60;

	if (!options.skipModelPicker && !options.modelOverride && !isCustomGateway) {
		const models = await listModelsForProvider(provider, apiKey, baseUrl);
		if (models.length > 1 && models.length <= MAX_PICKABLE_MODELS) {
			model = await pickModel(models, {
				lastModel: config.providers[provider.id]?.lastModel,
				defaultModel: provider.defaultModel,
			});
		}
	}

	// Save last-selected model + provider + credentials so subsequent runs need
	// no env vars or re-prompting.
	if (!options.noConfig) {
		let updated = withLastModel(withLastProvider(config, provider.id), provider.id, model);
		updated = withBaseUrl(updated, provider.id, baseUrl);
		updated = withApiKey(updated, provider.id, apiKey);
		if (authToken) updated = withAuthToken(updated, provider.id, authToken);
		await saveConfig(updated);
	}

	// Build the stream function for this provider
	const streamFn = await buildStreamFunction(provider.id, {
		model,
		apiKey,
		baseUrl,
		authToken,
	});

	return { streamFn, model, apiKey: provider.requiresKey ? apiKey : undefined, baseUrl, authToken };
}

export async function buildStreamFunction(
	providerId: string,
	config: { model: string; apiKey: string; baseUrl?: string; authToken?: string },
): Promise<StreamFn> {
	switch (providerId) {
		case "faux": {
			const registration = registerFauxProvider();
			// Script a simple canned response so the faux CLI path actually
			// streams something (an empty script would produce a blank message).
			registration.setResponses([[fauxText("(faux) Hello! I'm friday-ng running on the mock provider — no API key needed.")]]);
			return createFauxStreamFn(registration);
		}
		case "ollama":
			return createOllamaStreamFn({ model: config.model, baseUrl: config.baseUrl });
		case "anthropic":
		case "claude":
			return createAnthropicStreamFn({
				model: config.model,
				apiKey: config.apiKey,
				// Some gateways (e.g. local proxies) authenticate via
				// `Authorization: Bearer` instead of / in addition to `x-api-key`.
				authToken: config.authToken,
				baseUrl: config.baseUrl,
			});
		case "google":
			return createGoogleStreamFn({ model: config.model, apiKey: config.apiKey, baseUrl: config.baseUrl });
		case "openai":
		case "groq":
		case "openrouter":
		case "deepseek":
		case "mistral":
		case "together":
		case "kilo":
			return createOpenAICompatStreamFn({ model: config.model, apiKey: config.apiKey, baseUrl: config.baseUrl });
		case "freecc":
			return createFreeccStreamFn({ model: config.model, apiKey: config.apiKey, baseUrl: config.baseUrl });
		default:
			throw new Error(`No implementation for provider: ${providerId}`);
	}
}

import { getConfigFilePath } from "./config.ts";
