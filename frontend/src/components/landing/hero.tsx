import { ArrowRightIcon } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const STATS = [
  { label: "Countries Covered", value: "150+" },
  { label: "Verified Sources", value: "1,000s" },
  { label: "Years of Methodology", value: "20+" },
  { label: "Research Domains", value: "4" },
];

export function Hero({ className }: { className?: string }) {
  return (
    <section
      className={cn(
        "relative w-full overflow-hidden bg-gradient-to-b from-[#fdf2f3] via-white to-white pt-32 pb-20 md:pt-40 md:pb-28",
        className,
      )}
    >
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 -z-0 opacity-60 [background:radial-gradient(60%_60%_at_50%_0%,#fbf5ec_0%,transparent_70%)]"
      />
      <div className="container-md relative z-10 mx-auto flex flex-col items-center px-6 text-center">
        <div className="inline-flex items-center gap-2 rounded-full border border-[#7b1e2b]/20 bg-[#fdf2f3] px-4 py-1.5 text-xs font-medium tracking-wide text-[#7b1e2b] uppercase">
          <span className="size-1.5 rounded-full bg-[#7b1e2b]" />
          AI-Powered Intelligence by WRI
        </div>

        <h1 className="mt-6 max-w-4xl text-balance text-4xl font-semibold leading-[1.1] tracking-tight text-[#0a1628] md:text-6xl">
          Analyst-quality research,{" "}
          <span className="text-[#7b1e2b]">conversationally.</span>
        </h1>

        <p className="mt-6 max-w-2xl text-balance text-lg leading-relaxed text-[#4b5563] md:text-xl">
          WRI AI brings World Research Institute&rsquo;s analyst methodology to
          a chat interface. Ask about markets, industries, trends, or
          competitors &mdash; every claim is backed by a verified-source
          citation chain.
        </p>

        <div className="mt-10 flex flex-col items-center gap-3 sm:flex-row sm:gap-4">
          <Button
            asChild
            size="lg"
            className="h-12 bg-[#7b1e2b] px-8 text-base font-semibold text-white shadow-lg shadow-[#7b1e2b]/20 transition-all hover:bg-[#9a2a39] hover:shadow-xl hover:shadow-[#7b1e2b]/25"
          >
            <Link href="/workspace">
              Start a Research Session
              <ArrowRightIcon className="ml-1 size-4" />
            </Link>
          </Button>
          <Button
            asChild
            variant="outline"
            size="lg"
            className="h-12 border-[#0a1628]/15 bg-white px-8 text-base font-medium text-[#0a1628] hover:border-[#0a1628]/30 hover:bg-[#faf7f2]"
          >
            <Link href="#how-it-works">How it works</Link>
          </Button>
        </div>

        <p className="mt-6 text-xs text-[#6b7280]">
          For WRI clients and partners. Sign-in required.
        </p>

        <div className="mt-20 grid w-full max-w-3xl grid-cols-2 gap-x-8 gap-y-6 border-t border-[#e5e7eb] pt-10 md:grid-cols-4">
          {STATS.map((stat) => (
            <div key={stat.label} className="flex flex-col items-center">
              <div className="text-3xl font-semibold tracking-tight text-[#0a1628] md:text-4xl">
                {stat.value}
              </div>
              <div className="mt-1 text-xs font-medium tracking-wide text-[#6b7280] uppercase">
                {stat.label}
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
