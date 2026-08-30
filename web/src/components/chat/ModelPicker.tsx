"use client";

import { Bot, Check, ChevronDown } from "lucide-react";
import type { ProviderInfo } from "@/lib/types";

export function ModelPicker({
	open,
	provider,
	model,
	providers,
	onPick,
	onToggle,
}: {
	open: boolean;
	provider: string;
	model: string;
	providers: ProviderInfo[];
	onPick: (providerId: string, modelId: string) => void;
	onToggle: () => void;
}) {
	return (
		<div className="harness-model-strip">
			<button type="button" className="harness-model-chip" onClick={onToggle} aria-haspopup="listbox" aria-expanded={open}>
				<Bot size={14} />
				<span>{model}</span>
				<ChevronDown size={14} />
			</button>
			{open && (
				<div className="harness-model-popover" role="listbox">
					{providers.map((option) => (
						<button
							key={option.id}
							type="button"
							onClick={() => onPick(option.id, option.defaultModel)}
							role="option"
							aria-selected={option.id === provider}
						>
							<span>{option.name}</span>
							<small>{option.defaultModel}</small>
							{option.id === provider && <Check size={15} />}
						</button>
					))}
				</div>
			)}
		</div>
	);
}
