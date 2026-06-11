import { redirect } from "next/navigation";
import { type ReactNode } from "react";

import { GatewayOfflineFallback } from "@/components/workspace/gateway-offline-fallback";
import { AuthProvider } from "@/core/auth/AuthProvider";
import { getServerSideUser } from "@/core/auth/server";
import { assertNever } from "@/core/auth/types";

export const dynamic = "force-dynamic";

export default async function AuthLayout({
  children,
}: {
  children: ReactNode;
}) {
  const result = await getServerSideUser();

  switch (result.tag) {
    case "authenticated":
      redirect("/workspace");
    case "needs_setup":
      // Allow access to setup page
      return <AuthProvider initialUser={result.user}>{children}</AuthProvider>;
    case "system_setup_required":
    case "unauthenticated":
      return <AuthProvider initialUser={null}>{children}</AuthProvider>;
    case "gateway_unavailable":
      // Auth pages have no banner of their own, so render one here. The
      // fallback's AuthProvider replaces the bare-HTML branch that
      // previously locked users out without any logout/retry capability.
      return (
        <GatewayOfflineFallback renderBanner>
          <div className="relative flex min-h-screen flex-col items-center justify-center gap-4 bg-gradient-to-b from-[#fdf2f3] via-white to-[#fbf5ec] px-6 text-[#0a1628]">
            <h2 className="text-xl font-semibold tracking-tight text-[#0a1628]">
              Service temporarily unavailable
            </h2>
            <p className="max-w-md text-center text-sm text-[#4b5563]">
              WRI AI is briefly unreachable. The offline banner will retry
              automatically and restore your session the moment it&apos;s back.
            </p>
          </div>
        </GatewayOfflineFallback>
      );
    case "config_error":
      throw new Error(result.message);
    default:
      assertNever(result);
  }
}
