#!/usr/bin/env node
/**
 * friday-ng CLI — a streaming AI assistant built on the Pi Agent Harness
 * streaming architecture.
 *
 * Usage:
 *   friday-ng "Hello, what is 2+2?"
 *   friday-ng "Write a haiku" --provider openai
 *   friday-ng "Hello" --provider ollama
 *
 * First run with a real provider: paste your API key, pick a model, and it's saved.
 * Subsequent runs auto-detect and use your saved key + model.
 */
import { Agent } from "./agent.ts";
import { ConsoleRenderer } from "./console-renderer.ts";
import { Tui } from "./tui.ts";
import { loadConfig, saveConfig, withLastModel } from "./config.ts";
import { setupProvider, listModelsForProvider, buildStreamFunction } from "./interactive.ts";
import { findProvider, listProviders, resolveApiKey, type ProviderMeta } from "./providers/registry.ts";
import { isOllamaRunning } from "./providers/ollama.ts";
import type { Model } from "./types.ts";

interface CliOptions {
	prompt: string;
	provider?: string;
	model?: string;
	apiKey?: string;
	listProviders: boolean;
	listModels: boolean;
	help: boolean;
	noConfig: boolean;
	forceKey: boolean;
	repl: boolean;
}

function parseArgs(argv: string[]): CliOptions {
	const opts: CliOptions = {
		prompt: "",
		listProviders: false,
		listModels: false,
		help: false,
		noConfig: false,
		forceKey: false,
		repl: false,
	};
	const positional: string[] = [];

	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--help" || arg === "-h") opts.help = true;
		else if (arg === "--provider") opts.provider = argv[++i];
		else if (arg === "--model") opts.model = argv[++i];
		else if (arg === "--api-key") opts.apiKey = argv[++i];
		else if (arg === "--list-providers") opts.listProviders = true;
		else if (arg === "--list-models") opts.listModels = true;
		else if (arg === "--repl" || arg === "-i") opts.repl = true;
		else if (arg === "--no-config") opts.noConfig = true;
		else if (arg === "--force-key") opts.forceKey = true;
		else if (!arg?.startsWith("--")) positional.push(arg ?? "");
	}

	opts.prompt = positional.join(" ").trim();
	return opts;
}

function printHelp(): void {
	const providers = listProviders()
		.map((p) => `  ${p.id.padEnd(12)} — ${p.name}: ${p.description}`)
		.join("\n");

	console.log(`friday-ng — next-gen AI assistant with instant token streaming

USAGE:
  friday-ng <prompt> [options]

OPTIONS:
  --provider <id>     Provider to use (default: openai, or saved from last run)
  --model <name>      Model name (skip picker, use this directly)
  --api-key <key>     API key (skip prompt, don't save to config)
  --list-providers    Print all supported providers and exit
  --list-models       Print available models for the selected provider
  --no-config         Don't save API key or model to config
  --force-key         Re-prompt for API key even if one is saved
  --repl, -i          Interactive TUI (Pi-style chat) instead of one-shot
  --help, -h          Show this help

PROVIDERS:
${providers}

EXAMPLES:
  friday-ng "What is 2+2?"                              # first run: paste key, pick model
  friday-ng "Hello" --provider ollama                    # local Ollama, no key needed
  friday-ng "What is 2+2?" --provider openai             # OpenAI, uses saved key
  friday-ng "Tell me a joke" --provider anthropic         # Claude
  friday-ng --provider claude -i                           # interactive Pi-style chat (TUI)
  friday-ng "Explain gravity" --provider google             # Gemini
  friday-ng "Hello" --provider kilo                        # Kilo.ai gateway
  KILO_BASE_URL=https://api.kilo.ai/api/openrouter friday-ng "Hi" --provider kilo

API keys are stored in ~/.friday-ng/config.json (mode 0600).
For non-canonical providers, set <PROVIDER>_BASE_URL env var before running.`);
}

async function listAvailableModels(providerId: string, apiKey?: string): Promise<void> {
	const provider = findProvider(providerId);
	if (!provider) {
		console.error(`Unknown provider: ${providerId}`);
		process.exit(1);
	}
	const config = await loadConfig();
	const key = apiKey ?? resolveApiKey(providerId) ?? config.providers[providerId]?.apiKey ?? "";
	const baseUrl = config.providers[providerId]?.baseUrl ?? provider.defaultBaseUrl;

	const models = await listModelsForProvider(provider, key, baseUrl);
	if (models.length === 0) {
		console.log(`(Could not fetch model list from ${provider.name})`);
		console.log(`Default: ${provider.defaultModel}`);
	} else {
		for (const m of models) console.log(m);
	}
}

/** Built-in tools for the CLI. */
const cliTools = [
	{
		name: "calculator",
		description: "Evaluate a simple arithmetic expression and return the result.",
		parameters: {
			type: "object" as const,
			properties: {
				expression: { type: "string" as const, description: "The arithmetic expression to evaluate" },
			},
			required: ["expression"],
		},
		execute: async (_id: string, params: any) => {
			try {
				const result = Function(`"use strict"; return (${params.expression})`)() as number;
				return {
					content: [{ type: "text" as const, text: String(result) }],
					details: { result },
				};
			} catch (e) {
				return {
					content: [{ type: "text" as const, text: `Error: ${e instanceof Error ? e.message : String(e)}` }],
					details: { error: true },
					terminate: false,
				};
			}
		},
	},
	{
		name: "websearch",
		description: "Search the web for information.",
		parameters: {
			type: "object" as const,
			properties: {
				query: { type: "string" as const, description: "Search query" },
			},
			required: ["query"],
		},
		execute: async (_id: string, params: any) => {
			return {
				content: [{ type: "text" as const, text: `Search results for: ${params.query}` }],
				details: { simulated: true },
			};
		},
	},
];

async function main(): Promise<void> {
	const opts = parseArgs(process.argv.slice(2));

	if (opts.help) {
		printHelp();
		return;
	}

	if (opts.listProviders) {
		for (const p of listProviders()) {
			console.log(`${p.id}\t${p.name}\t${p.requiresKey ? "key" : "no-key"}`);
		}
		return;
	}

	if (opts.listModels) {
		const providerId = opts.provider ?? "openai";
		await listAvailableModels(providerId, opts.apiKey);
		return;
	}

	if (!opts.prompt && !opts.repl) {
		printHelp();
		process.exit(0);
	}

	// Resolve provider: explicit flag → saved config → "openai" → "faux"
	let providerId = opts.provider;
	if (!providerId) {
		const config = await loadConfig();
		providerId = config.lastProvider ?? "openai";
	}

	// Pre-flight: if Ollama, check if it's running
	if (providerId === "ollama") {
		const running = await isOllamaRunning();
		if (!running) {
			console.error(
				"✗ Ollama is not running. Start it with: ollama serve\n" +
					"  Or use a different provider: --provider openai, --provider anthropic, etc.",
			);
			process.exit(1);
		}
	}

	// Auto-setup: prompt for key if needed, pick model if needed
	const setup = await setupProvider(providerId, {
		modelOverride: opts.model,
		noConfig: opts.noConfig,
		forceKeyPrompt: opts.forceKey,
	});

	// Build the model object for the agent
	const providerMeta = findProvider(providerId)!;
	const model = buildModelObject(providerMeta, setup.model);

	if (opts.repl) {
		await runRepl(opts, setup, providerId, model);
		return;
	}

	const agent = new Agent({
		initialState: {
			systemPrompt: `You are friday-ng, a next-generation AI assistant with instant token streaming. Current model: ${setup.model}. Be helpful, concise, and friendly.`,
			tools: cliTools as any,
			model,
		},
		streamFunction: setup.streamFn,
		toolExecution: "sequential",
	});

	const renderer = new ConsoleRenderer({ showThinking: false });
	agent.subscribe((event) => renderer.render(event));

	await agent.prompt(opts.prompt);
	await agent.waitForIdle();
}

/** Build the `Model` object the Agent loop needs from provider meta + id. */
function buildModelObject(provider: ProviderMeta, modelId: string): Model {
	return {
		id: modelId,
		name: modelId,
		api: provider.id as any,
		provider: provider.id as any,
		baseUrl: provider.defaultBaseUrl,
		reasoning: false,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		contextWindow: 8192,
		maxTokens: 4096,
	};
}

/** Interactive REPL mode: a living Pi-style TUI instead of one-shot. */
async function runRepl(
	opts: CliOptions,
	setup: Awaited<ReturnType<typeof setupProvider>>,
	providerId: string,
	model: Model,
): Promise<void> {
	const providerMeta = findProvider(providerId)!;
	const agent = new Agent({
		initialState: {
			systemPrompt: `You are friday-ng, a next-generation AI assistant with instant token streaming. Current model: ${setup.model}. Be helpful, concise, and friendly.`,
			tools: cliTools as any,
			model,
		},
		streamFunction: setup.streamFn,
		toolExecution: "sequential",
	});

	const tui = new Tui({
		model: setup.model,
		provider: providerId,
		onSubmit: async (text: string) => {
			await agent.prompt(text);
			await agent.waitForIdle();
		},
		onQuit: () => agent.abort(),
		onListModels: async () => {
			const config = await loadConfig();
			const baseUrl = config.providers[providerId]?.baseUrl ?? providerMeta.defaultBaseUrl;
			const apiKey = config.providers[providerId]?.apiKey ?? "";
			try {
				return await listModelsForProvider(providerMeta, apiKey, baseUrl);
			} catch {
				return [];
			}
		},
		onSelectModel: async (id: string) => {
			const streamFn = await buildStreamFunction(providerId, {
				model: id,
				apiKey: setup.apiKey ?? "",
				baseUrl: setup.baseUrl,
				authToken: setup.authToken,
			});
			agent.useModel(buildModelObject(providerMeta, id), streamFn);
			tui.setModel(id);
			const config = await loadConfig();
			await saveConfig(withLastModel(config, providerId, id));
		},
	});

	agent.subscribe((event) => tui.handleEvent(event));
	await tui.run(opts.prompt);
}

void main().catch((err) => {
	console.error("Fatal error:", err);
	process.exit(1);
});
