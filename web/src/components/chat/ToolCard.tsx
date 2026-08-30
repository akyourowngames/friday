"use client";

import { ChevronDown, Copy, FileCode2, Globe2, Terminal, Wrench } from "lucide-react";
import type { ToolRun } from "@/lib/types";

const ICON_FOR_CATEGORY = {
	shell: Terminal,
	file: FileCode2,
	search: Globe2,
	other: Wrench,
} as const;

export function ToolCard({ tool, onToggle }: { tool: ToolRun; onToggle: () => void }) {
	const Icon = ICON_FOR_CATEGORY[tool.category];
	const args = typeof tool.args === "object" ? JSON.stringify(tool.args) : String(tool.args ?? "");
	const onCopy = (e: React.MouseEvent) => {
		e.stopPropagation();
		if (typeof navigator !== "undefined" && navigator.clipboard) {
			navigator.clipboard.writeText(tool.output).catch(() => undefined);
		}
	};

	return (
		<section className={`harness-tool-card is-${tool.category} ${tool.expanded ? "is-open" : ""}`}>
			<button type="button" className="harness-tool-summary" onClick={onToggle} aria-expanded={tool.expanded}>
				<span className={`harness-status-dot is-${tool.status}`} />
				<Icon size={15} strokeWidth={1.8} />
				<span className="harness-tool-name">{tool.name}</span>
				<span className="harness-tool-args">{args || "running"}</span>
				<ChevronDown size={15} className="harness-tool-chevron" />
			</button>
			{tool.expanded && (
				<div className="harness-tool-detail">
					<div className="harness-tool-detail-head">
						<span>Output</span>
						<button type="button" onClick={onCopy} title="Copy output" aria-label="Copy output">
							<Copy size={14} />
						</button>
					</div>
					<pre>{tool.output || "Awaiting tool result…"}</pre>
				</div>
			)}
		</section>
	);
}
