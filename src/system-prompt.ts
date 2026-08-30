import { buildEnvironmentContext } from "./env-context.ts";
import { loadProfile, loadProjectFile } from "./profile.ts";

/** Build the shared harness system prompt for CLI, TUI, and web hosts. */
export async function buildSystemPrompt(modelId: string, workspace = process.cwd()): Promise<string> {
	const [profile, project] = await Promise.all([loadProfile(), loadProjectFile(workspace)]);
	return (
		`You are friday-ng, a next-generation AI assistant with instant token streaming. ` +
		`Current model: ${modelId}. Be helpful, concise, and friendly.` +
		(profile ? `\n\n## About the user\n${profile.trim()}\n` : "") +
		(project ? `\n\n## About this project\n${project.trim()}\n` : "") +
		buildEnvironmentContext()
	);
}
