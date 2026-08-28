/**
 * Provider registry — catalog metadata for every LLM provider friday-ng supports.
 *
 * Design: `defaultBaseUrl` holds each provider's canonical, verified
 * OpenAI-compatible endpoint. These were checked against the official docs
 * (kilo.ai, groq.com, deepseek.com, mistral.ai, together.ai, openrouter.ai)
 * so we never ship a made-up URL. `<PROVIDER>_BASE_URL` still overrides.
 *
 * The streaming implementation is in sibling files (openai-compat.ts, anthropic.ts, google.ts, ollama.ts).
 * This file is just the lookup table.
 */

export type ApiStyle = "openai" | "anthropic" | "gemini" | "ollama" | "faux";

export interface ProviderMeta {
	/** Canonical id used in --provider flag and config keys. */
	id: string;
	/** Human-readable display name. */
	name: string;
	/** Env var(s) to check for the API key, in priority order. */
	apiKeyEnvVars: string[];
	/** Optional env var to override baseUrl. */
	baseUrlEnvVar?: string;
	/**
	 * Default base URL — ONLY set for providers where we're certain of the canonical URL.
	 * If empty/undefined, the SDK's own default is used, or `<PROVIDER>_BASE_URL` env var.
	 */
	defaultBaseUrl: string;
	/** Default model to use if user hasn't picked one. */
	defaultModel: string;
	/** Which streaming adapter to use. */
	apiStyle: ApiStyle;
	/** Whether this provider requires a key. Local providers may skip this. */
	requiresKey: boolean;
	/** Where the user can get an API key. */
	keyUrl: string;
	/** Short description for help output. */
	description: string;
}

const PROVIDERS: ProviderMeta[] = [
	{
		id: "faux",
		name: "Mock (testing)",
		apiKeyEnvVars: [],
		defaultBaseUrl: "",
		defaultModel: "faux-1",
		apiStyle: "faux",
		requiresKey: false,
		keyUrl: "",
		description: "Built-in mock provider. Streams fake responses without needing an API key.",
	},
	{
		id: "openai",
		name: "OpenAI",
		apiKeyEnvVars: ["OPENAI_API_KEY"],
		baseUrlEnvVar: "OPENAI_BASE_URL",
		defaultBaseUrl: "https://api.openai.com/v1",
		defaultModel: "gpt-4o-mini",
		apiStyle: "openai",
		requiresKey: true,
		keyUrl: "https://platform.openai.com/api-keys",
		description: "OpenAI's GPT-4o, o1, and friends.",
	},
	{
		id: "anthropic",
		name: "Anthropic (Claude)",
		apiKeyEnvVars: ["ANTHROPIC_API_KEY"],
		baseUrlEnvVar: "ANTHROPIC_BASE_URL",
		defaultBaseUrl: "https://api.anthropic.com",
		defaultModel: "claude-3-5-sonnet-latest",
		apiStyle: "anthropic",
		requiresKey: true,
		keyUrl: "https://console.anthropic.com/settings/keys",
		description: "Anthropic's Claude 3.5 Sonnet, Haiku, and Opus models.",
	},
	{
		// Alias for the Anthropic provider — same adapter, invoked as `claude`.
		id: "claude",
		name: "Claude (Anthropic)",
		apiKeyEnvVars: ["ANTHROPIC_API_KEY"],
		baseUrlEnvVar: "ANTHROPIC_BASE_URL",
		defaultBaseUrl: "https://api.anthropic.com",
		defaultModel: "claude-3-5-sonnet-latest",
		apiStyle: "anthropic",
		requiresKey: true,
		keyUrl: "https://console.anthropic.com/settings/keys",
		description: "Anthropic's Claude models (alias for `anthropic`).",
	},
	{
		id: "google",
		name: "Google Gemini",
		apiKeyEnvVars: ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
		baseUrlEnvVar: "GOOGLE_BASE_URL",
		defaultBaseUrl: "https://generativelanguage.googleapis.com/v1beta",
		defaultModel: "gemini-2.0-flash",
		apiStyle: "gemini",
		requiresKey: true,
		keyUrl: "https://aistudio.google.com/app/apikey",
		description: "Google's Gemini 2.0 Flash, Pro, and Flash-Lite models.",
	},
	{
		id: "ollama",
		name: "Ollama (local)",
		apiKeyEnvVars: [],
		baseUrlEnvVar: "OLLAMA_BASE_URL",
		defaultBaseUrl: "http://localhost:11434/v1",
		defaultModel: "llama3.2",
		apiStyle: "ollama",
		requiresKey: false,
		keyUrl: "",
		description: "Local LLMs via Ollama. No API key needed, just run `ollama serve`.",
	},
	// The rest are OpenAI-compatible providers whose canonical base URLs
	// (verified against official docs) are set below.
	{
		id: "groq",
		name: "Groq",
		apiKeyEnvVars: ["GROQ_API_KEY"],
		baseUrlEnvVar: "GROQ_BASE_URL",
		defaultBaseUrl: "https://api.groq.com/openai/v1",
		defaultModel: "llama-3.1-70b-versatile",
		apiStyle: "openai",
		requiresKey: true,
		keyUrl: "https://console.groq.com/keys",
		description: "Groq ultra-fast inference (OpenAI-compatible).",
	},
	{
		id: "openrouter",
		name: "OpenRouter",
		apiKeyEnvVars: ["OPENROUTER_API_KEY"],
		baseUrlEnvVar: "OPENROUTER_BASE_URL",
		defaultBaseUrl: "https://openrouter.ai/api/v1",
		defaultModel: "anthropic/claude-3.5-sonnet",
		apiStyle: "openai",
		requiresKey: true,
		keyUrl: "https://openrouter.ai/keys",
		description: "OpenRouter — gateway to 100+ models (OpenAI-compatible).",
	},
	{
		id: "deepseek",
		name: "DeepSeek",
		apiKeyEnvVars: ["DEEPSEEK_API_KEY"],
		baseUrlEnvVar: "DEEPSEEK_BASE_URL",
		defaultBaseUrl: "https://api.deepseek.com/v1",
		defaultModel: "deepseek-chat",
		apiStyle: "openai",
		requiresKey: true,
		keyUrl: "https://platform.deepseek.com/api_keys",
		description: "DeepSeek — strong coding/reasoning (OpenAI-compatible).",
	},
	{
		id: "mistral",
		name: "Mistral",
		apiKeyEnvVars: ["MISTRAL_API_KEY"],
		baseUrlEnvVar: "MISTRAL_BASE_URL",
		defaultBaseUrl: "https://api.mistral.ai/v1",
		defaultModel: "mistral-large-latest",
		apiStyle: "openai",
		requiresKey: true,
		keyUrl: "https://console.mistral.ai/api-keys/",
		description: "Mistral models (OpenAI-compatible).",
	},
	{
		id: "together",
		name: "Together AI",
		apiKeyEnvVars: ["TOGETHER_API_KEY"],
		baseUrlEnvVar: "TOGETHER_BASE_URL",
		defaultBaseUrl: "https://api.together.ai/v1",
		defaultModel: "meta-llama/Llama-3-70b-chat-hf",
		apiStyle: "openai",
		requiresKey: true,
		keyUrl: "https://api.together.xyz/settings/api-keys",
		description: "Together AI — open-source models (OpenAI-compatible).",
	},
	{
		id: "kilo",
		name: "Kilo.ai",
		apiKeyEnvVars: ["KILO_API_KEY"],
		baseUrlEnvVar: "KILO_BASE_URL",
		defaultBaseUrl: "https://api.kilo.ai/api/gateway",
		defaultModel: "tencent/hy3:free",
		apiStyle: "openai",
		requiresKey: true,
		keyUrl: "https://kilo.ai",
		description: "Kilo.ai gateway — OpenAI-compatible (https://api.kilo.ai/api/gateway).",
	},
];

const PROVIDER_MAP = new Map(PROVIDERS.map((p) => [p.id, p] as const));

/** Look up a provider by id. Throws if not found. */
export function getProvider(id: string): ProviderMeta {
	const p = PROVIDER_MAP.get(id);
	if (!p) {
		throw new Error(
			`Unknown provider: "${id}". Available: ${PROVIDERS.map((p) => p.id).join(", ")}`,
		);
	}
	return p;
}

/** Try to look up a provider by id. Returns undefined if not found. */
export function findProvider(id: string): ProviderMeta | undefined {
	return PROVIDER_MAP.get(id);
}

/** List all providers. */
export function listProviders(): ProviderMeta[] {
	return PROVIDERS.slice();
}

/** Resolve the effective API key for a provider (env). Returns undefined if none. */
export function resolveApiKey(providerId: string): string | undefined {
	const provider = findProvider(providerId);
	if (!provider || !provider.apiKeyEnvVars.length) return undefined;
	for (const envVar of provider.apiKeyEnvVars) {
		const v = process.env[envVar];
		if (v) return v;
	}
	return undefined;
}

/** Resolve the effective base URL for a provider.
 *  Priority: env var > provider default > "" (let SDK default). */
export function resolveBaseUrl(providerId: string): string {
	const provider = findProvider(providerId);
	if (!provider) return "";
	if (provider.baseUrlEnvVar) {
		const fromEnv = process.env[provider.baseUrlEnvVar];
		if (fromEnv) return fromEnv;
	}
	return provider.defaultBaseUrl;
}
