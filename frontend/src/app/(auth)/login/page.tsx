"use client";

import Image from "next/image";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/core/auth/AuthProvider";
import { parseAuthError } from "@/core/auth/types";

/**
 * Validate next parameter
 * Prevent open redirect attacks
 * Per RFC-001: Only allow relative paths starting with /
 */
function validateNextParam(next: string | null): string | null {
  if (!next) {
    return null;
  }
  if (!next.startsWith("/")) {
    return null;
  }
  if (
    next.startsWith("//") ||
    next.startsWith("http://") ||
    next.startsWith("https://")
  ) {
    return null;
  }
  if (next.includes(":") && !next.startsWith("/")) {
    return null;
  }
  return next;
}

export default function LoginPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { isAuthenticated } = useAuth();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isLogin, setIsLogin] = useState(true);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const nextParam = searchParams.get("next");
  const redirectPath = validateNextParam(nextParam) ?? "/workspace";

  // Redirect if already authenticated (client-side, post-login)
  useEffect(() => {
    if (isAuthenticated) {
      router.push(redirectPath);
    }
  }, [isAuthenticated, redirectPath, router]);

  // Redirect to setup if the system has no users yet
  useEffect(() => {
    let cancelled = false;
    void fetch("/api/v1/auth/setup-status")
      .then((r) => r.json())
      .then((data: { needs_setup?: boolean }) => {
        if (!cancelled && data.needs_setup) {
          router.push("/setup");
        }
      })
      .catch(() => {
        // Ignore errors; user stays on login page
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const endpoint = isLogin
        ? "/api/v1/auth/login/local"
        : "/api/v1/auth/register";
      const body = isLogin
        ? `username=${encodeURIComponent(email)}&password=${encodeURIComponent(password)}`
        : JSON.stringify({ email, password });

      const headers: HeadersInit = isLogin
        ? { "Content-Type": "application/x-www-form-urlencoded" }
        : { "Content-Type": "application/json" };

      const res = await fetch(endpoint, {
        method: "POST",
        headers,
        body,
        credentials: "include",
      });

      if (!res.ok) {
        const data = await res.json();
        const authError = parseAuthError(data);
        setError(authError.message);
        return;
      }

      router.push(redirectPath);
    } catch {
      setError("Network error. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative flex min-h-screen w-full items-center justify-center overflow-hidden bg-gradient-to-b from-[#fdf2f3] via-white to-[#fbf5ec] px-6 py-12 text-[#0a1628]">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 [background:radial-gradient(60%_60%_at_50%_0%,#fbf5ec_0%,transparent_70%)]"
      />
      <div className="relative z-10 w-full max-w-md">
        <Link
          href="/"
          className="mb-8 inline-flex items-center gap-3 transition-opacity hover:opacity-80"
        >
          <Image
            src="/wri/android-chrome-192x192.png"
            alt="World Research Institute"
            width={40}
            height={40}
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

        <div className="rounded-2xl border border-[#e5e7eb] bg-white p-8 shadow-sm">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-[#0a1628]">
              {isLogin ? "Sign in to WRI AI" : "Create your account"}
            </h1>
            <p className="mt-2 text-sm text-[#4b5563]">
              {isLogin
                ? "Continue your research where you left off."
                : "Get started with conversational intelligence backed by WRI methodology."}
            </p>
          </div>

          <form onSubmit={handleSubmit} className="mt-6 space-y-4">
            <div className="flex flex-col gap-1.5">
              <label
                htmlFor="email"
                className="text-xs font-medium tracking-wide text-[#374151] uppercase"
              >
                Email
              </label>
              <Input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                required
                autoComplete="email"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label
                htmlFor="password"
                className="text-xs font-medium tracking-wide text-[#374151] uppercase"
              >
                Password
              </label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="•••••••"
                required
                minLength={isLogin ? 6 : 8}
                autoComplete={isLogin ? "current-password" : "new-password"}
              />
            </div>

            {error && (
              <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                {error}
              </p>
            )}

            <Button
              type="submit"
              className="h-11 w-full bg-[#7b1e2b] text-base font-semibold text-white hover:bg-[#9a2a39]"
              disabled={loading}
            >
              {loading
                ? "Please wait…"
                : isLogin
                  ? "Sign In"
                  : "Create Account"}
            </Button>
          </form>

          <div className="mt-6 text-center text-sm text-[#4b5563]">
            <button
              type="button"
              onClick={() => {
                setIsLogin(!isLogin);
                setError("");
              }}
              className="font-medium text-[#7b1e2b] underline-offset-4 hover:underline"
            >
              {isLogin
                ? "Don't have an account? Sign up"
                : "Already have an account? Sign in"}
            </button>
          </div>
        </div>

        <div className="mt-6 text-center text-xs text-[#6b7280]">
          <Link
            href="/"
            className="font-medium text-[#4b5563] underline-offset-4 hover:text-[#0a1628] hover:underline"
          >
            ← Back to home
          </Link>
        </div>
      </div>
    </div>
  );
}
