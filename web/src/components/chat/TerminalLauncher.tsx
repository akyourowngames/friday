"use client";

import { useEffect, useState } from "react";
import { Terminal } from "lucide-react";
import { getWorkspaceInfo, revealInTerminal } from "@/lib/run-client";
import { toast } from "sonner";

/**
 * "Open Terminal" button. On click:
 *  1. Asks the server for the current working directory.
 *  2. Asks the server to launch a new native OS terminal at that
 *     directory (`start "" cmd /K cd /d ...` on Windows; `open -a
 *     Terminal` on macOS; $TERMINAL on Linux).
 *  3. Shows a toast confirming the action or a fallback hint if the
 *     server couldn't launch the terminal (sandboxed environments).
 */
export function TerminalLauncher() {
	const [cwd, setCwd] = useState<string>("");
	const [busy, setBusy] = useState(false);

	useEffect(() => {
		getWorkspaceInfo()
			.then((info) => setCwd(info.cwd))
			.catch(() => undefined);
	}, []);

	const onClick = async () => {
		if (busy) return;
		setBusy(true);
		try {
			const info = cwd ? { cwd } : await getWorkspaceInfo();
			const result = await revealInTerminal(info.cwd);
			toast.success(`Opened terminal in ${result.cwd}`, {
				description: "A new system terminal window has been launched.",
			});
		} catch (e) {
			toast.error("Could not open terminal", {
				description: (e as Error).message,
			});
		} finally {
			setBusy(false);
		}
	};

	const tooltip = cwd ? `Open terminal in ${cwd}` : "Open terminal in workspace";

	return (
		<button
			type="button"
			className="harness-icon-button"
			onClick={onClick}
			title={tooltip}
			aria-label={tooltip}
			disabled={busy}
		>
			<Terminal size={17} />
		</button>
	);
}
