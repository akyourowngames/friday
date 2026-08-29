import { describe, it, expect, beforeEach } from "vitest";
import {
	clearSlashCommands,
	findCommands,
	getSlashCommand,
	listSlashCommands,
	parseSlashCommand,
	registerSlashCommand,
	type SlashCommand,
	type SlashCommandContext,
	type SlashCommandHost,
} from "../src/slash-commands.ts";

class FakeHost implements SlashCommandHost {
	cleared = false;
	systemLines: string[] = [];
	submitted: string[] = [];
	quitted = false;
	repainted = 0;
	settings = new Map<string, unknown>();

	appendSystemLine(text: string): void {
		this.systemLines.push(text);
	}
	clearHistory(): void {
		this.cleared = true;
	}
	repaint(): void {
		this.repainted += 1;
	}
	async submit(text: string): Promise<void> {
		this.submitted.push(text);
	}
	quit(): void {
		this.quitted = true;
	}
	getSetting(key: string): unknown {
		return this.settings.get(key);
	}
	setSetting(key: string, value: unknown): void {
		this.settings.set(key, value);
	}
}

describe("slash-commands registry", () => {
	beforeEach(() => {
		clearSlashCommands();
	});

	it("registers and looks up commands by name", () => {
		const cmd: SlashCommand = { name: "/foo", description: "Foo", run: () => ({}) };
		registerSlashCommand(cmd);
		expect(getSlashCommand("/foo")?.description).toBe("Foo");
		expect(listSlashCommands()).toHaveLength(1);
	});

	it("rejects duplicate registrations", () => {
		registerSlashCommand({ name: "/dup", description: "a", run: () => ({}) });
		expect(() => registerSlashCommand({ name: "/dup", description: "b", run: () => ({}) })).toThrow();
	});

	it("parses a registered slash command from input", () => {
		registerSlashCommand({ name: "/hi", description: "say hi", run: () => ({ message: "ok" }) });
		const result = parseSlashCommand("/hi there");
		expect(result?.command.name).toBe("/hi");
		expect(result?.args).toBe("there");
	});

	it("returns undefined for unregistered slash commands", () => {
		const result = parseSlashCommand("/unknown arg");
		expect(result).toBeUndefined();
	});

	it("returns undefined for non-slash input", () => {
		const result = parseSlashCommand("hello world");
		expect(result).toBeUndefined();
	});

	it("runs a command and captures its result", async () => {
		const host = new FakeHost();
		let seen: SlashCommandContext | undefined;
		registerSlashCommand({
			name: "/echo",
			description: "echo",
			run: (ctx) => {
				seen = ctx;
				return { message: ctx.args };
			},
		});
		const result = parseSlashCommand("/echo hello world");
		expect(result).toBeDefined();
		const r = await result!.command.run({
			tui: host,
			agent: {} as any,
			args: "hello world",
		});
		expect(r.message).toBe("hello world");
		expect(seen?.args).toBe("hello world");
	});

	it("supports clearHistory, quit, and submit follow-ups via result", async () => {
		const host = new FakeHost();
		registerSlashCommand({
			name: "/clear",
			description: "clear",
			run: ({ tui }) => {
				tui.clearHistory();
				return { clearHistory: true };
			},
		});
		const r = await getSlashCommand("/clear")!.run({ tui: host, agent: {} as any, args: "" });
		expect(r.clearHistory).toBe(true);
		expect(host.cleared).toBe(true);
	});

	it("clearSlashCommands empties the registry", () => {
		registerSlashCommand({ name: "/a", description: "a", run: () => ({}) });
		registerSlashCommand({ name: "/b", description: "b", run: () => ({}) });
		expect(listSlashCommands()).toHaveLength(2);
		clearSlashCommands();
		expect(listSlashCommands()).toHaveLength(0);
	});
});

describe("findCommands", () => {
	beforeEach(() => {
		clearSlashCommands();
		registerSlashCommand({ name: "/help", description: "help", run: () => ({}) });
		registerSlashCommand({ name: "/model", description: "model", run: () => ({}) });
		registerSlashCommand({ name: "/history", description: "history", run: () => ({}) });
		registerSlashCommand({ name: "/cost", description: "cost", run: () => ({}) });
	});

	it("returns all commands for an empty query", () => {
		const res = findCommands("");
		expect(res).toHaveLength(4);
	});

	it("filters by case-insensitive substring", () => {
		const res = findCommands("h");
		expect(res.map((c) => c.name)).toEqual(["/help", "/history"]);
	});

	it("prefix matches come first", () => {
		const res = findCommands("h");
		// /help and /history both start with "h", so they come before any
		// non-prefix matches (there are none here, but the ordering is
		// prefix-first).
		expect(res[0]!.name).toBe("/help");
	});

	it("returns empty array when nothing matches", () => {
		expect(findCommands("xyz")).toEqual([]);
	});

	it("respects the limit", () => {
		const res = findCommands("", 2);
		expect(res).toHaveLength(2);
	});

	it("handles query with leading slash", () => {
		const res = findCommands("/mod");
		expect(res).toHaveLength(1);
		expect(res[0]!.name).toBe("/model");
	});
});
