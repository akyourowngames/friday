import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "FRIDAY",
  description: "A dark Jarvis-inspired assistant interface."
};

type RootLayoutProps = Readonly<{
  children: React.ReactNode;
}>;

export default function RootLayout({ children }: RootLayoutProps) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
