/**
 * Example friday-ng extension: registers a `/hello` slash command and a
 * `pre_tool_use` hook that logs every tool call to the console.
 *
 * Drop this into `~/.friday-ng/extensions/` and friday-ng will pick it up
 * automatically on startup.
 */
export default (host) => {
	host.commands.register({
		name: "/hello",
		description: "Say hi from an extension.",
		run: ({ args }) => ({ message: `hello, ${args || "world"}!` }),
	});

	host.hooks.on("pre_tool_use", (payload) => {
		host.log(`[hello-world] tool call: ${payload.tool.name}`);
	});
};
