import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "Aurex AI — Intelligent Trading System",
    template: "%s | Aurex AI",
  },
  description:
    "Institutional-grade algorithmic trading powered by AI. Connect your MT5 account and let Aurex AI manage your trades with precision.",
  keywords: ["algorithmic trading", "MT5", "AI trading", "forex", "automated trading"],
  authors: [{ name: "Aurex AI" }],
  openGraph: {
    type: "website",
    title: "Aurex AI — Intelligent Trading System",
    description: "Institutional-grade algorithmic trading powered by AI.",
    siteName: "Aurex AI",
  },
  icons: {
    icon: "/favicon.svg",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      </head>
      <body className="h-full bg-white text-slate-900 antialiased">{children}</body>
    </html>
  );
}
