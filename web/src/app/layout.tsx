import type { Metadata, Viewport } from "next";
import "./globals.css";
import { ToasterMount } from "@/components/toaster-mount";
import { ThemeProvider } from "@/components/theme-provider";

export const metadata: Metadata = {
	title: "HarNESs · friday-ng",
	description: "A local, multi-provider AI coding agent with streaming tool calls.",
	applicationName: "friday-ng",
	authors: [{ name: "friday-ng contributors" }],
};

export const viewport: Viewport = {
	themeColor: "#0a0a0d",
	width: "device-width",
	initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
	return (
		<html lang="en" className="dark" suppressHydrationWarning>
			<body className="h-screen w-screen overflow-hidden bg-background font-sans text-foreground antialiased">
				<ThemeProvider>
					{children}
					<ToasterMount />
				</ThemeProvider>
			</body>
		</html>
	);
}
