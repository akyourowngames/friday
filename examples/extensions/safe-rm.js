/**
 * Example friday-ng extension: veto any `bash` call that includes the
 * destructive `rm -rf` flag.
 *
 * Drop into `~/.friday-ng/extensions/`.
 */
export default (host) => {
	host.hooks.on("pre_tool_use", (payload) => {
		if (payload.tool.name !== "bash") return;
		const cmd = String(payload.args?.command ?? "");
		if (/rm\s+-rf?\s+/.test(cmd)) {
			return { ...payload, cancel: true, reason: "blocked: rm -rf is not allowed" };
		}
	});
};
