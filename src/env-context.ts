/**
 * Environment context injected into the system prompt so the model knows
 * where it is without spending tool calls discovering it: OS, shell, cwd,
 * and the current date/time.
 */
import * as os from "node:os";

/** Describe the shell the bash tool actually uses on this platform. */
export function shellDescription(): { os: string; shell: string } {
	if (process.platform === "win32") {
		return { os: `Windows ${os.release()} (${process.arch})`, shell: "cmd.exe (NOT POSIX)" };
	}
	if (process.platform === "darwin") return { os: `macOS ${os.release()}`, shell: "/bin/sh (POSIX)" };
	if (process.platform === "linux") return { os: `Linux ${os.release()}`, shell: "/bin/sh (POSIX)" };
	return { os: `${process.platform} ${os.release()}`, shell: "/bin/sh" };
}

/** Build the environment block appended to the system prompt. */
export function buildEnvironmentContext(now: Date = new Date()): string {
	const { os: osDesc, shell } = shellDescription();
	const date = now.toLocaleDateString("en-US", {
		weekday: "long",
		year: "numeric",
		month: "long",
		day: "numeric",
	});
	const time = now.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
	const tz = Intl.DateTimeFormat().resolvedOptions().timeZone ?? "unknown";
	return [
		"",
		"## Environment",
		`- Operating system: ${osDesc}`,
		`- Shell used by the bash tool: ${shell}`,
		`- Current working directory: ${process.cwd()}`,
		`- Current date and time: ${date} at ${time} (${tz})`,
		"",
		"Use this environment information directly — e.g. you already know today's date, so never run a command just to find it out. Match shell syntax to the platform when running commands.",
	].join("\n");
}

