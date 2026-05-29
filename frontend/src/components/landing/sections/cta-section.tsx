import { ArrowRightIcon } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";

export function CTASection() {
  return (
    <section className="relative w-full overflow-hidden bg-[#0a1628] py-20 md:py-28">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 [background:radial-gradient(50%_50%_at_50%_100%,rgba(123,30,43,0.35)_0%,transparent_70%)]"
      />
      <div className="container-md relative mx-auto flex flex-col items-center px-6 text-center">
        <h2 className="max-w-3xl text-balance text-3xl font-semibold leading-tight tracking-tight text-white md:text-5xl">
          Ready to start your first research session?
        </h2>
        <p className="mt-6 max-w-xl text-balance text-base leading-relaxed text-white/70 md:text-lg">
          Sign in to open your workspace. Your first query takes about thirty
          seconds.
        </p>

        <div className="mt-10 flex flex-col items-center gap-3 sm:flex-row sm:gap-4">
          <Button
            asChild
            size="lg"
            className="h-12 bg-[#7b1e2b] px-8 text-base font-semibold text-white shadow-lg shadow-[#7b1e2b]/30 transition-all hover:bg-[#9a2a39] hover:shadow-xl hover:shadow-[#7b1e2b]/40"
          >
            <Link href="/workspace">
              Open Workspace
              <ArrowRightIcon className="ml-1 size-4" />
            </Link>
          </Button>
          <Button
            asChild
            variant="outline"
            size="lg"
            className="h-12 border-white/30 bg-transparent px-8 text-base font-medium text-white hover:border-white/50 hover:bg-white/5 hover:text-white"
          >
            <a
              href="https://www.worldresearch.org/contact/"
              target="_blank"
              rel="noopener noreferrer"
            >
              Talk to WRI
            </a>
          </Button>
        </div>

        <p className="mt-6 text-xs text-white/50">
          Not a WRI client yet?{" "}
          <a
            href="https://www.worldresearch.org/services/"
            target="_blank"
            rel="noopener noreferrer"
            className="font-medium text-white/80 underline-offset-4 hover:text-white hover:underline"
          >
            Explore WRI&rsquo;s services
          </a>
          .
        </p>
      </div>
    </section>
  );
}
