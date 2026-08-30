"use client";

import { Moon, Sun } from "lucide-react";

export function ThemeToggle({
	theme,
	onChange,
}: {
	theme: "dark" | "light";
	onChange: (next: "dark" | "light") => void;
}) {
	return (
		<button
			type="button"
			className="harness-icon-button"
			onClick={() => onChange(theme === "dark" ? "light" : "dark")}
			title={theme === "dark" ? "Switch to light" : "Switch to dark"}
			aria-label="Toggle theme"
		>
			{theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
		</button>
	);
}
