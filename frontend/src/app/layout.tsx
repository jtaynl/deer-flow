import "katex/dist/katex.min.css";
import "@/styles/globals.css";

import { type Metadata } from "next";

import { ThemeProvider } from "@/components/theme-provider";
import { I18nProvider } from "@/core/i18n/context";
import { detectLocaleServer } from "@/core/i18n/server";

export const metadata: Metadata = {
  title: "WRI AI — Intelligence Assistant by World Research Institute",
  description:
    "AI-powered intelligence on demand. Conversational research backed by World Research Institute's analyst methodology, verified-source citations, and 150+ country coverage.",
  icons: {
    icon: [
      { url: "/wri/favicon-32x32.png", sizes: "32x32", type: "image/png" },
      { url: "/wri/favicon-16x16.png", sizes: "16x16", type: "image/png" },
    ],
    apple: "/wri/apple-touch-icon.png",
  },
  manifest: "/wri/site.webmanifest",
};

export default async function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const locale = await detectLocaleServer();
  return (
    <html lang={locale} suppressContentEditableWarning suppressHydrationWarning>
      <body>
        <ThemeProvider attribute="class" enableSystem disableTransitionOnChange>
          <I18nProvider initialLocale={locale}>{children}</I18nProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
