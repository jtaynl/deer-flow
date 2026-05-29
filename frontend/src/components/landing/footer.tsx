import Image from "next/image";
import Link from "next/link";

import { cn } from "@/lib/utils";

const FOOTER_LINKS = {
  WRI: [
    { label: "About", href: "https://www.worldresearch.org/about/" },
    { label: "Services", href: "https://www.worldresearch.org/services/" },
    { label: "Insights", href: "https://www.worldresearch.org/insights/" },
    { label: "Contact", href: "https://www.worldresearch.org/contact/" },
  ],
  Product: [
    { label: "Capabilities", href: "#capabilities" },
    { label: "How it works", href: "#how-it-works" },
    { label: "Why WRI AI", href: "#trust" },
    { label: "Open workspace", href: "/workspace" },
  ],
};

export function Footer({ className }: { className?: string }) {
  const year = new Date().getFullYear();
  return (
    <footer className={cn("w-full bg-[#0a1628] text-white", className)}>
      <div className="container-md mx-auto px-6 py-16 md:py-20">
        <div className="grid grid-cols-1 gap-12 md:grid-cols-12">
          <div className="md:col-span-5">
            <Link href="/" className="inline-flex items-center gap-3">
              <Image
                src="/wri/android-chrome-192x192-white.png"
                alt="World Research Institute"
                width={40}
                height={40}
                className="rounded-sm"
              />
              <div className="flex flex-col leading-tight">
                <span className="text-base font-semibold tracking-tight text-white">
                  WRI AI
                </span>
                <span className="text-[10px] uppercase tracking-wider text-white/60">
                  World Research Institute
                </span>
              </div>
            </Link>
            <p className="mt-6 max-w-md text-sm leading-relaxed text-white/70">
              AI-powered intelligence on demand. WRI AI brings the methodology
              of the World Research Institute to a conversational interface
              backed by verified-source citations.
            </p>
            <p className="mt-6 text-xs text-white/50">
              Empowering decisions through world intelligence.
            </p>
          </div>

          <div className="md:col-span-3 md:col-start-7">
            <div className="text-xs font-semibold tracking-[0.2em] text-white/50 uppercase">
              World Research Institute
            </div>
            <ul className="mt-5 space-y-3">
              {FOOTER_LINKS.WRI.map((link) => (
                <li key={link.label}>
                  <a
                    href={link.href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-sm text-white/80 transition-colors hover:text-white"
                  >
                    {link.label} ↗
                  </a>
                </li>
              ))}
            </ul>
          </div>

          <div className="md:col-span-3">
            <div className="text-xs font-semibold tracking-[0.2em] text-white/50 uppercase">
              WRI AI
            </div>
            <ul className="mt-5 space-y-3">
              {FOOTER_LINKS.Product.map((link) => (
                <li key={link.label}>
                  <Link
                    href={link.href}
                    className="text-sm text-white/80 transition-colors hover:text-white"
                  >
                    {link.label}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        </div>

        <div className="mt-16 flex flex-col items-start justify-between gap-4 border-t border-white/10 pt-8 text-xs text-white/50 md:flex-row md:items-center">
          <div>
            © {year} World Research Institute. All rights reserved.
          </div>
          <div className="flex items-center gap-5">
            <a
              href="https://www.linkedin.com/"
              target="_blank"
              rel="noopener noreferrer"
              aria-label="LinkedIn"
              className="text-white/60 transition-colors hover:text-white"
            >
              LinkedIn
            </a>
            <a
              href="https://twitter.com/"
              target="_blank"
              rel="noopener noreferrer"
              aria-label="X (Twitter)"
              className="text-white/60 transition-colors hover:text-white"
            >
              𝕏
            </a>
            <a
              href="https://www.worldresearch.org/"
              target="_blank"
              rel="noopener noreferrer"
              className="text-white/60 transition-colors hover:text-white"
            >
              worldresearch.org ↗
            </a>
          </div>
        </div>
      </div>
    </footer>
  );
}
