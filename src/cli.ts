#!/usr/bin/env node
/**
 * friday-ng CLI — a streaming AI assistant built on the Pi Agent Harness
 * streaming architecture.
 *
 * Usage:
 *   friday-ng "Hello, what is 2+2?"
 *   friday-ng "Write a haiku" --provider openai
 *
 * By default, uses a built-in "faux" provider that simulates streaming
 * responses without needing API keys. With --provider openai, requires
 * the OPENAI_API_KEY env var.
 */
import { Agent } from "./agent.ts";
import { ConsoleRenderer } from "./console-renderer.ts";
import { registerFauxProvider, createFauxStreamFn, fauxText, fauxToolCall } from "./provider-faux.ts";
import type { AgentEvent, AgentMessage, AgentTool } from "./types.ts";

interface CliOptions {
	prompt: string;
	provider: "faux" | "openai";
	model: string;
	help: boolean;
}

function parseArgs(argv: string[]): CliOptions {
	const opts: CliOptions = { prompt: "", provider: "faux", model: "faux-1", help: false };
	const positional: string[] = [];

	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--help" || arg === "-h") {
			opts.help = true;
		} else if (arg === "--provider") {
			opts.provider = argv[++i] as "faux" | "openai";
		} else if (arg === "--model") {
			opts.model = argv[++i]!;
		} else if (arg === "--thinking") {
			opts.provider = opts.provider; // no-op placeholder
		} else if (!arg.startsWith("--")) {
			positional.push(arg);
		}
	}

	opts.prompt = positional.join(" ");
	return opts;
}

function printHelp(): void {
	console.log(`friday-ng — next-gen AI assistant with instant token streaming

USAGE:
  friday-ng <prompt> [options]

OPTIONS:
  --provider <name>   LLM provider: "faux" (default, no API key needed) or "openai"
  --model <name>      Model name (default: faux-1)
  --help, -h          Show this help

EXAMPLES:
  friday-ng "What is 2+2?"
  friday-ng "Write a haiku" --provider openai --model gpt-4o-mini

By default uses a mock provider that simulates streaming. The mock provider
responds to tool calls with "calculator" and "websearch" tools.`);
}

/** Built-in tools for the CLI. */
const cliTools: AgentTool[] = [
	{
		name: "calculator",
		description: "Evaluate a simple arithmetic expression and return the result.",
		parameters: {
			type: "object" as const,
			properties: {
				expression: { type: "string" as const, description: "The arithmetic expression to evaluate" },
			},
			required: ["expression"],
		} as any,
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
		} as any,
		execute: async (_id: string, params: any) => {
			// Placeholder — returns a simulated result
			return {
				content: [{ type: "text" as const, text: `Search results for: ${params.query}` }],
				details: { simulated: true },
			};
		},
	},
];

/** Set up the faux provider with a scripted response based on the prompt. */
function setupFauxProvider(prompt: string) {
	const registration = registerFauxProvider({ tokensPerSecond: 100 });

	// Script a response: if the prompt mentions "calculate" or a math question,
	// simulate a tool call. Otherwise just return a text answer.
	const lowerPrompt = prompt.toLowerCase();
	if (lowerPrompt.includes("calculator") || /what.*is.*\d/.test(lowerPrompt)) {
		registration.setResponses([
			[
				fauxToolCall("calculator", { expression: "2 + 2" }),
				fauxText("The answer is 4."),
			],
		]);
	} else {
		registration.setResponses([[fauxText(`I'm friday-ng, built on the Pi Agent Harness streaming architecture. You said: ${prompt}`)]]);
	}

	return createFauxStreamFn(registration);
}

async function main(): Promise<void> {
	const opts = parseArgs(process.argv.slice(2));

	if (opts.help || !opts.prompt) {
		printHelp();
		process.exit(opts.prompt ? 0 : 0);
		return;
	}

	let streamFunction: any;

	if (opts.provider === "openai") {
		const { OPENAI_API_KEY } = process.env;
		if (!OPENAI_API_KEY) {
			console.error("Error: --provider openai requires OPENAI_API_KEY env var.\nSet it or use the default 'faux' provider.");
			process.exit(1);
		}
		// Lazy-load the OpenAI provider only when needed
		const { createOpenAIStreamFn } = await import("./provider-openai.ts");
		streamFunction = createOpenAIStreamFn({ model: opts.model, apiKey: OPENAI_API_KEY });
	} else {
		streamFunction = setupFauxProvider(opts.prompt);
	}

	const agent = new Agent({
		initialState: {
			systemPrompt: "You are friday-ng, a next-generation AI assistant with instant token streaming. Be helpful, concise, and friendly.",
			tools: cliTools,
		},
		streamFunction,
		toolExecution: "sequential",
	});

	const renderer = new ConsoleRenderer({ showThinking: true });
	agent.subscribe(listenerForRenderer(renderer));

	await agent.prompt(opts.prompt);
	await agent.waitForIdle();
}

	function listenerForRenderer(renderer: ConsoleRenderer) {
		return (event: AgentEvent, _signal: AbortSignal | undefined) => {
			renderer.render(event);
		};
	}

void main().catch((err) => {
	console.error("Fatal error:", err);
	process.exit(1);
});
