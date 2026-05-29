import Image from "next/image";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import type { Locale } from "@/core/i18n/locale";
import { cn } from "@/lib/utils";

export type HeaderProps = {
  className?: string;
  // Accepted for legacy compatibility with blog/docs layouts; WRI header is
  // always English and always points to "/" for home.
  homeURL?: string;
  locale?: Locale;
};

export function Header({ className }: HeaderProps) {
  return (
    <header
      className={cn(
        "fixed top-0 right-0 left-0 z-30 mx-auto flex h-16 items-center justify-between border-b border-[#e5e7eb] bg-white/80 backdrop-blur-md",
        className,
      )}
    >
      <div className="container-md mx-auto flex w-full items-center justify-between px-6">
        <Link
          href="/"
          className="flex items-center gap-3 transition-opacity hover:opacity-80"
        >
          <Image
            src="/wri/android-chrome-192x192.png"
            alt="World Research Institute"
            width={36}
            height={36}
            className="rounded-sm"
            priority
          />
          <div className="flex flex-col leading-tight">
            <span className="text-base font-semibold tracking-tight text-[#0a1628]">
              WRI AI
            </span>
            <span className="text-[10px] uppercase tracking-wider text-[#7b1e2b]">
              World Research Institute
            </span>
          </div>
        </Link>

        <nav className="hidden items-center gap-8 text-sm font-medium text-[#4b5563] md:flex">
          <Link
            href="#capabilities"
            className="transition-colors hover:text-[#0a1628]"
          >
            Capabilities
          </Link>
          <Link
            href="#how-it-works"
            className="transition-colors hover:text-[#0a1628]"
          >
            How it works
          </Link>
          <Link
            href="#trust"
            className="transition-colors hover:text-[#0a1628]"
          >
            Why WRI AI
          </Link>
          <a
            href="https://www.worldresearch.org/insights/"
            target="_blank"
            rel="noopener noreferrer"
            className="transition-colors hover:text-[#0a1628]"
          >
            Insights ↗
          </a>
        </nav>

        <div className="flex items-center gap-3">
          <Link
            href="/workspace"
            className="hidden text-sm font-medium text-[#4b5563] transition-colors hover:text-[#0a1628] sm:inline-block"
          >
            Sign In
          </Link>
          <Button
            asChild
            size="sm"
            className="bg-[#7b1e2b] text-white hover:bg-[#9a2a39]"
          >
            <Link href="/workspace">Start Research</Link>
          </Button>
        </div>
      </div>
    </header>
  );
}
