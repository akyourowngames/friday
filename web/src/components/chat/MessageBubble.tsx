"use client";

import { Volume2 } from "lucide-react";
import type { ChatMessage, ToolRun } from "@/lib/types";
import { ToolCard } from "./ToolCard";

/** Drop duplicate tool cards (defensive — the reducer already dedupes). */
function dedupeToolsById(tools: ToolRun[] | undefined): ToolRun[] {
	if (!tools || tools.length < 2) return tools ?? [];
	const seen = new Set<string>();
	const out: ToolRun[] = [];
	for (const tool of tools) {
		if (seen.has(tool.id)) continue;
		seen.add(tool.id);
		out.push(tool);
	}
	return out;
}

export function MessageBubble({
	message,
	onToggleTool,
	onSpeak,
}: {
	message: ChatMessage;
	onToggleTool: (messageId: string, toolId: string) => void;
	onSpeak: (text: string) => void;
}) {
	const isUser = message.role === "user";
	const showTyping = !isUser && message.status === "streaming" && !message.text && (message.tools?.length ?? 0) === 0;

	return (
		<article className={`harness-message ${isUser ? "is-user" : ""}`}>
			{!isUser && (
				<div className="harness-assistant-label">
					<span className="harness-mini-mark" />
					HarNESs
				</div>
			)}
			<div className={isUser ? "harness-message-user-body" : undefined}>
				{message.text && <div className="harness-message-text">{message.text}</div>}
				{dedupeToolsById(message.tools).map((tool) => (
					<ToolCard
						key={`${message.id}::${tool.id}`}
						tool={tool}
						onToggle={() => onToggleTool(message.id, tool.id)}
					/>
				))}
				{showTyping && (
					<div className="harness-typing">
						<i />
						<i />
						<i />
					</div>
				)}
			</div>
			{!isUser && message.status === "done" && message.text && (
				<button type="button" className="harness-speak-message" onClick={() => onSpeak(message.text)}>
					<Volume2 size={14} />
					Read aloud
				</button>
			)}
		</article>
	);
}
