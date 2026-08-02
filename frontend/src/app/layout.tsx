import "@/styles/globals.css";

import { type Metadata } from "next";

import { ThemeProvider } from "@/components/theme-provider";
import { DEFAULT_LOCALE } from "@/core/i18n/locale";

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

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang={DEFAULT_LOCALE}
      suppressContentEditableWarning
      suppressHydrationWarning
    >
      <body>
        <ThemeProvider attribute="class" enableSystem disableTransitionOnChange>
          {children}
        </ThemeProvider>
      </body>
    </html>
  );
}
