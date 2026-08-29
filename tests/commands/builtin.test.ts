import { describe, it, expect, beforeEach } from "vitest";
import {
	computeUsageTotals,
	formatUsageTotals,
	makeClearCommand,
	makeCompactCommand,
	makeCostCommand,
	makeExitCommand,
	makeHelpCommand,
	makeModelCommand,
	makeProviderCommand,
	makeQuitCommand,
	makeReloadCommand,
	makeResumeCommand,
	makeSessionsCommand,
	makeSettingsCommand,
	makeToolsCommand,
	makeUsageCommand,
} from "../../src/commands/builtin.ts";
import {
	clearSlashCommands,
	getSlashCommand,
	listSlashCommands,
	parseSlashCommand,
	registerSlashCommand,
} from "../../src/slash-commands.ts";
import type { Agent } from "../../src/agent.ts";
import type { SlashCommandHost } from "../../src/slash-commands.ts";

class FakeHost implements SlashCommandHost {
	cleared = false;
	systemLines: string[] = [];
	quitted = false;
	settings = new Map<string, unknown>();
	appendSystemLine(t: string) {
		this.systemLines.push(t);
	}
	clearHistory() {
		this.cleared = true;
	}
	repaint() {}
	async submitFollowup() {}
	quitTui() {
		this.quitted = true;
	}
	getSetting(k: string) {
		return this.settings.get(k);
	}
	setSetting(k: string, v: unknown) {
		this.settings.set(k, v);
	}
}

function makeFakeAgent(): Agent {
	const messages: any[] = [
		{ role: "user", content: "hi", timestamp: 1 },
		{
			role: "assistant",
			content: [
				{ type: "text", text: "hello" },
				{ type: "toolCall", id: "t1", name: "calc", arguments: { expression: "2+2" } },
			],
			usage: { input: 5, output: 3, cacheRead: 0, cacheWrite: 0, totalTokens: 8 },
			stopReason: "toolUse",
			timestamp: 2,
		},
		{
			role: "toolResult",
			toolCallId: "t1",
			toolName: "calc",
			content: [{ type: "text", text: "4" }],
			isError: false,
			timestamp: 3,
		},
		{
			role: "assistant",
			content: [{ type: "text", text: "=4" }],
			usage: { input: 7, output: 2, cacheRead: 1, cacheWrite: 0, totalTokens: 9 },
			stopReason: "stop",
			timestamp: 4,
		},
	];
	return { state: { messages } } as unknown as Agent;
}

describe("builtin commands", () => {
	let host: FakeHost;
	let agent: Agent;

	beforeEach(() => {
		clearSlashCommands();
		host = new FakeHost();
		agent = makeFakeAgent();
	});

	it("/help lists every registered command", () => {
		registerSlashCommand(makeHelpCommand());
		registerSlashCommand(makeExitCommand());
		const cmd = getSlashCommand("/help")!;
		const r = cmd.run({ tui: host, agent, args: "" });
		expect(host.systemLines.length).toBe(1);
		expect(host.systemLines[0]).toContain("/help");
		expect(host.systemLines[0]).toContain("/exit");
	});

	it("/help <name> prints a single command", () => {
		registerSlashCommand(makeHelpCommand());
		registerSlashCommand(makeExitCommand());
		const r = getSlashCommand("/help")!.run({ tui: host, agent, args: "/exit" }) as any;
		expect(r.message).toContain("/exit");
		expect(r.message).toContain("Quit friday-ng");
	});

	it("/help <name> errors for unknown command", () => {
		registerSlashCommand(makeHelpCommand());
		const r = getSlashCommand("/help")!.run({ tui: host, agent, args: "/nope" }) as any;
		expect(r.message).toContain("Unknown command");
	});

	it("/exit quits the host", () => {
		registerSlashCommand(makeExitCommand());
		const r = getSlashCommand("/exit")!.run({ tui: host, agent, args: "" }) as any;
		expect(r.quit).toBe(true);
		expect(host.quitted).toBe(true);
	});

	it("/quit is an alias for /exit", () => {
		registerSlashCommand(makeQuitCommand());
		const r = getSlashCommand("/quit")!.run({ tui: host, agent, args: "" }) as any;
		expect(r.quit).toBe(true);
	});

	it("/clear clears the host's history", () => {
		registerSlashCommand(makeClearCommand());
		const r = getSlashCommand("/clear")!.run({ tui: host, agent, args: "" }) as any;
		expect(r.clearHistory).toBe(true);
		expect(host.cleared).toBe(true);
	});

	it("/cost reports usage totals", () => {
		registerSlashCommand(makeCostCommand());
		getSlashCommand("/cost")!.run({ tui: host, agent, args: "" });
		expect(host.systemLines[0]).toContain("Turns:      2");
		expect(host.systemLines[0]).toContain("Tool calls: 1");
		expect(host.systemLines[0]).toContain("12"); // total input (5+7)
	});

	it("/usage is an alias for /cost", () => {
		registerSlashCommand(makeUsageCommand());
		getSlashCommand("/usage")!.run({ tui: host, agent, args: "" });
		expect(host.systemLines[0]).toContain("Turns:");
	});

	it("/model picks the first available model", async () => {
		const cmd = makeModelCommand({
			listModels: async () => ["m1", "m2"],
			onSelect: (id) => {
				host.setSetting("lastPick", id);
			},
		});
		registerSlashCommand(cmd);
		const r = (await getSlashCommand("/model")!.run({ tui: host, agent, args: "" })) as any;
		expect(r.message).toContain("m1");
		expect(host.getSetting("lastPick")).toBe("m1");
	});

	it("/model reports empty list gracefully", async () => {
		const cmd = makeModelCommand({ listModels: async () => [], onSelect: () => {} });
		registerSlashCommand(cmd);
		const r = (await getSlashCommand("/model")!.run({ tui: host, agent, args: "" })) as any;
		expect(r.message).toContain("No models");
	});

	it("/settings calls the host open callback", async () => {
		let opened = false;
		const cmd = makeSettingsCommand({ onOpenSettings: async () => { opened = true; } });
		registerSlashCommand(cmd);
		await getSlashCommand("/settings")!.run({ tui: host, agent, args: "" });
		expect(opened).toBe(true);
	});

	it("/reload calls the host reload callback", async () => {
		let reloaded = false;
		const cmd = makeReloadCommand({ onReload: async () => { reloaded = true; } });
		registerSlashCommand(cmd);
		const r = await getSlashCommand("/reload")!.run({ tui: host, agent, args: "" });
		expect(reloaded).toBe(true);
		expect((r as any).message).toContain("reloaded");
	});

	it("/provider with no args shows current + available", () => {
		const cmd = makeProviderCommand({
			onSwitch: () => {},
			currentProvider: "openai",
			listProviders: () => ["openai", "anthropic"],
		});
		registerSlashCommand(cmd);
		const r = getSlashCommand("/provider")!.run({ tui: host, agent, args: "" }) as any;
		expect(r.message).toContain("openai");
		expect(r.message).toContain("anthropic");
	});

	it("/provider with no args handles sync return type correctly", () => {
		const cmd = makeProviderCommand({
			onSwitch: () => {},
			currentProvider: "openai",
			listProviders: () => ["openai", "anthropic"],
		});
		registerSlashCommand(cmd);
		const r = getSlashCommand("/provider")!.run({ tui: host, agent, args: "" });
		// Should be a sync object, not a promise
		expect(r).not.toBeInstanceOf(Promise);
	});

	it("/provider with an arg switches provider", async () => {
		let switched = "";
		const cmd = makeProviderCommand({
			onSwitch: async (id) => { switched = id; },
			currentProvider: "openai",
			listProviders: () => ["openai", "anthropic"],
		});
		registerSlashCommand(cmd);
		await getSlashCommand("/provider")!.run({ tui: host, agent, args: "anthropic" });
		expect(switched).toBe("anthropic");
	});

	it("/tools lists the registered tools", () => {
		const cmd = makeToolsCommand({ onList: () => ["bash", "read", "write"] });
		registerSlashCommand(cmd);
		const r = getSlashCommand("/tools")!.run({ tui: host, agent, args: "" }) as any;
		expect(r.message).toContain("bash");
		expect(r.message).toContain("write");
	});

	it("/compact calls the host compact callback", async () => {
		let called = false;
		const cmd = makeCompactCommand({ onCompact: async () => { called = true; } });
		registerSlashCommand(cmd);
		await getSlashCommand("/compact")!.run({ tui: host, agent, args: "" });
		expect(called).toBe(true);
	});

	it("/sessions lists saved sessions", async () => {
		const cmd = makeSessionsCommand({ onList: async () => [
			{ id: "s1", title: "My first session", updatedAt: new Date().toISOString(), messageCount: 3 },
			{ id: "s2", title: "Coding session", updatedAt: new Date().toISOString(), messageCount: 7 },
		] });
		registerSlashCommand(cmd);
		const r = await getSlashCommand("/sessions")!.run({ tui: host, agent, args: "" });
		expect((r as any).message).toContain("My first session");
		expect((r as any).message).toContain("Coding session");
	});

	it("/resume with no args asks which session", async () => {
		const cmd = makeResumeCommand({
			listSessions: async () => [
				{ id: "s1", title: "One", updatedAt: new Date().toISOString(), messageCount: 1 },
				{ id: "s2", title: "Two", updatedAt: new Date().toISOString(), messageCount: 2 },
			],
			onResume: () => {},
		});
		registerSlashCommand(cmd);
		const r = await getSlashCommand("/resume")!.run({ tui: host, agent, args: "" });
		expect((r as any).message).toContain("One");
	});

	it("/resume with an id resumes that session", async () => {
		let resumed = "";
		const cmd = makeResumeCommand({
			listSessions: async () => [
				{ id: "s1", title: "One", updatedAt: new Date().toISOString(), messageCount: 1 },
				{ id: "s2", title: "Two", updatedAt: new Date().toISOString(), messageCount: 2 },
			],
			onResume: async (id) => { resumed = id; },
		});
		registerSlashCommand(cmd);
		await getSlashCommand("/resume")!.run({ tui: host, agent, args: "s2" });
		expect(resumed).toBe("s2");
	});

	it("/resume accepts a 1-based index to pick a session", async () => {
		let resumed = "";
		const cmd = makeResumeCommand({
			listSessions: async () => [
				{ id: "aa", title: "Alpha", updatedAt: new Date().toISOString(), messageCount: 1 },
				{ id: "bb", title: "Beta", updatedAt: new Date().toISOString(), messageCount: 2 },
			],
			onResume: async (id) => { resumed = id; },
		});
		registerSlashCommand(cmd);
		await getSlashCommand("/resume")!.run({ tui: host, agent, args: "2" });
		expect(resumed).toBe("bb");
	});

	it("parseSlashCommand routes registered names", () => {
		registerSlashCommand(makeClearCommand());
		const parsed = parseSlashCommand("/clear");
		expect(parsed?.command.name).toBe("/clear");
	});

	it("computeUsageTotals sums assistant messages", () => {
		const totals = computeUsageTotals(agent);
		expect(totals.turns).toBe(2);
		expect(totals.toolCalls).toBe(1);
		expect(totals.input).toBe(12);
		expect(totals.output).toBe(5);
	});

	it("formatUsageTotals is human-readable", () => {
		const totals = computeUsageTotals(agent);
		const out = formatUsageTotals(totals);
		expect(out).toContain("Turns:      2");
		expect(out).toContain("Tool calls: 1");
	});
});
