"use client";

import { useEffect, useRef, useState } from "react";
import { Command, Mic, SendHorizontal, Square } from "lucide-react";

const SAMPLE_PROMPTS = [
	"Review this repository and find the risky parts",
	"Explain the current agent event stream",
	"Implement a safer permission policy",
	"What changed in the last session?",
] as const;

export function Composer({
	isStreaming,
	onSubmit,
	onAbort,
	disabled,
	externalDraft,
}: {
	isStreaming: boolean;
	isListening: boolean;
	onSubmit: (text: string) => Promise<void>;
	onAbort: () => Promise<void>;
	disabled: boolean;
	externalDraft?: string;
}) {
	const [input, setInput] = useState("");
	const [placeholder, setPlaceholder] = useState<string>(SAMPLE_PROMPTS[0]);
	const textareaRef = useRef<HTMLTextAreaElement | null>(null);

	// Rotate the placeholder text every few seconds for visual life.
	useEffect(() => {
		let i = 0;
		const timer = window.setInterval(() => {
			i = (i + 1) % SAMPLE_PROMPTS.length;
			setPlaceholder(SAMPLE_PROMPTS[i]);
		}, 4200);
		return () => window.clearInterval(timer);
	}, []);

	// Allow parent components to push a starter prompt into the composer
	// (e.g. clicking a sample card on the empty state).
	useEffect(() => {
		if (externalDraft !== undefined) {
			setInput(externalDraft);
			textareaRef.current?.focus();
		}
	}, [externalDraft]);

	// Auto-resize the textarea up to its 140px max-height.
	useEffect(() => {
		const el = textareaRef.current;
		if (!el) return;
		el.style.height = "auto";
		el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
	}, [input]);

	const send = async () => {
		const text = input.trim();
		if (!text || isStreaming) return;
		setInput("");
		await onSubmit(text);
	};

	return (
		<>
			<div className="harness-composer">
				<button
					type="button"
					className="harness-voice-button"
					title="Voice input (not yet implemented in browser)"
					aria-label="Voice input"
				>
					<Mic size={18} />
				</button>
				<textarea
					ref={textareaRef}
					value={input}
					onChange={(e) => setInput(e.target.value)}
					onKeyDown={(e) => {
						if (e.key === "Enter" && !e.shiftKey) {
							e.preventDefault();
							void send();
						}
					}}
					placeholder={placeholder}
					rows={1}
				/>
				{isStreaming ? (
					<button
						type="button"
						className="harness-send-button is-stop"
						onClick={() => void onAbort()}
						title="Stop agent"
						aria-label="Stop agent"
					>
						<Square size={15} fill="currentColor" />
					</button>
				) : (
					<button
						type="button"
						className="harness-send-button"
						onClick={() => void send()}
						disabled={disabled || !input.trim()}
						title="Send prompt"
						aria-label="Send prompt"
					>
						<SendHorizontal size={18} />
					</button>
				)}
			</div>
			<div className="harness-composer-hint">
				<span>
					<Command size={12} /> K command palette
				</span>
				<span>Enter to send · Shift Enter for newline</span>
			</div>
		</>
	);
}
