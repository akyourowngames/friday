"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowDown } from "lucide-react";
import type { ChatMessage } from "@/lib/types";
import { MessageBubble } from "./MessageBubble";

const STARTER_PROMPTS = [
	"Inspect this project",
	"Explain the architecture",
	"Plan a feature",
] as const;

export function MessageList({
	messages,
	isStreaming,
	onToggleTool,
	onPickStarter,
}: {
	messages: ChatMessage[];
	isStreaming: boolean;
	onToggleTool: (messageId: string, toolId: string) => void;
	onPickStarter?: (text: string) => void;
}) {
	const scrollRef = useRef<HTMLDivElement | null>(null);
	const [atBottom, setAtBottom] = useState(true);

	useEffect(() => {
		if (!atBottom) return;
		scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
	}, [messages, atBottom]);

	const onScroll = () => {
		const el = scrollRef.current;
		if (!el) return;
		setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 88);
	};

	const speak = (text: string) => {
		if (typeof window === "undefined") return;
		if (!("speechSynthesis" in window)) return;
		window.speechSynthesis.cancel();
		const utter = new SpeechSynthesisUtterance(text);
		window.speechSynthesis.speak(utter);
	};

	return (
		<>
			<div className="harness-conversation" ref={scrollRef} onScroll={onScroll}>
				<div className={`harness-thread ${messages.length === 0 ? "is-empty" : ""}`}>
					{messages.length === 0 ? (
						<div className="harness-empty">
							<div className="harness-orb">
								<i />
								<i />
								<i />
								<i />
								<i />
							</div>
							<p className="harness-eyebrow">HARNESS ONLINE</p>
							<h1>What are we building?</h1>
							<p>Ask the agent to inspect, reason, edit, search, and ship — every move stays visible.</p>
							<div className="harness-starter-row">
								{STARTER_PROMPTS.map((text) => (
									<button
										key={text}
										type="button"
										onClick={() => onPickStarter?.(text)}
									>
										{text}
									</button>
								))}
							</div>
						</div>
					) : (
						messages.map((m) => (
							<MessageBubble
								key={m.id}
								message={m}
								onToggleTool={onToggleTool}
								onSpeak={speak}
							/>
						))
					)}
				</div>
			</div>
			{!atBottom && messages.length > 0 && (
				<button
					type="button"
					className="harness-jump-bottom"
					onClick={() => {
						setAtBottom(true);
						scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
					}}
				>
					<ArrowDown size={15} />
					New message
				</button>
			)}
			{isStreaming && <div style={{ display: "none" }} aria-hidden="true" />}
		</>
	);
}
