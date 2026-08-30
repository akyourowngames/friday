import clsx, { type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
	return twMerge(clsx(inputs));
}

/** Humanize an ISO timestamp for the session list ("now", "5m", "3h", "2d"). */
export function relativeTime(iso: string): string {
	const minutes = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60_000));
	if (minutes < 2) return "now";
	if (minutes < 60) return `${minutes}m`;
	if (minutes < 1440) return `${Math.floor(minutes / 60)}h`;
	return `${Math.floor(minutes / 1440)}d`;
}
