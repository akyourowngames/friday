"use client";

import { ThemeProvider as NextThemesProvider } from "next-themes";
import type { ReactNode } from "react";

/**
 * Theme provider for next-themes. The sonner Toaster reads from
 * `useTheme()`, so this wrapper is required even though our shell
 * manages the actual `<html data-theme="...">` switch itself.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
	return (
		<NextThemesProvider attribute="class" defaultTheme="dark" enableSystem={false}>
			{children}
		</NextThemesProvider>
	);
}
