import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { AppShell } from "@/components/shell";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Sandcage", template: "%s · Sandcage" },
  description: "Give an agent a real task. Sandcage decides what it's allowed to touch.",
};

export const viewport: Viewport = { colorScheme: "dark", themeColor: "#0c0c0c", width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
