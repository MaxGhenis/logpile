import type { Metadata } from "next";
import {
  Playfair_Display,
  Plus_Jakarta_Sans,
  JetBrains_Mono,
} from "next/font/google";
import { Sidebar } from "@/components/sidebar";
import { config } from "@/lib/config";
import "./globals.css";

const brand = Playfair_Display({
  subsets: ["latin"],
  weight: ["700", "900"],
  variable: "--font-brand",
  display: "swap",
});

const body = Plus_Jakarta_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-body",
  display: "swap",
});

const mono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Logpile",
  description: "Stacked session memory — browse and analyze CC and Codex sessions",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${brand.variable} ${body.variable} ${mono.variable}`}
    >
      <body className="min-h-screen antialiased">
        <div className="flex min-h-screen">
          <Sidebar publicMode={config.publicMode} />
          <main className="flex-1 min-w-0 flex flex-col">
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}
