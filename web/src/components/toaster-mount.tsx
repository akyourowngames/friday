"use client";

import { Toaster } from "@/components/ui/sonner";

/**
 * Client wrapper around the sonner Toaster so the root server layout
 * can mount it without pulling next-themes' useTheme() into a server
 * context (which crashes the prerender).
 */
export function ToasterMount() {
	return <Toaster position="bottom-right" theme="dark" richColors closeButton />;
}
