"use client";

import Image from "next/image";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { RememberSessionOption } from "@/components/auth/remember-session-option";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getCsrfHeaders } from "@/core/api/fetcher";
import { useAuth } from "@/core/auth/AuthProvider";
import { loadRememberLoginPreference } from "@/core/auth/remember-login";
import {
  fetchSetupStatus,
  isSystemAlreadyInitializedError,
} from "@/core/auth/setup";
import { parseAuthError } from "@/core/auth/types";

type SetupMode = "loading" | "init_admin" | "change_password";

function BrandHeader() {
  return (
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
  );
}

function PageShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="relative flex min-h-screen w-full items-center justify-center overflow-hidden bg-gradient-to-b from-[#fdf2f3] via-white to-[#fbf5ec] px-6 py-12 text-[#0a1628]">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 [background:radial-gradient(60%_60%_at_50%_0%,#fbf5ec_0%,transparent_70%)]"
      />
      <div className="relative z-10 w-full max-w-md">{children}</div>
    </div>
  );
}

export default function SetupPage() {
  const router = useRouter();
  const { user, isAuthenticated } = useAuth();
  const [mode, setMode] = useState<SetupMode>("loading");

  const [email, setEmail] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [rememberMe, setRememberMe] = useState(
    () => loadRememberLoginPreference().rememberMe,
  );

  const [currentPassword, setCurrentPassword] = useState("");

  useEffect(() => {
    let cancelled = false;

    if (isAuthenticated && user?.needs_setup) {
      setMode("change_password");
    } else if (!isAuthenticated) {
      // Check if the system has no users yet
      void fetchSetupStatus()
        .then((data: { needs_setup?: boolean }) => {
          if (cancelled) return;
          if (data.needs_setup) {
            setMode("init_admin");
          } else {
            // System already set up and user is not logged in — go to login
            router.replace("/login");
          }
        })
        .catch(() => {
          if (!cancelled) router.replace("/login");
        });
    } else {
      // Authenticated but needs_setup is false — already set up
      router.replace("/workspace");
    }

    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, user, router]);

  const handleInitAdmin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (newPassword !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }

    setLoading(true);
    try {
      const res = await fetch("/api/v1/auth/initialize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          email,
          password: newPassword,
          remember_me: rememberMe,
        }),
      });

      if (!res.ok) {
        const data = await res.json();
        if (isSystemAlreadyInitializedError(data)) {
          router.replace("/login");
          return;
        }
        const authError = parseAuthError(data);
        setError(authError.message);
        return;
      }

      router.push("/workspace");
    } catch {
      setError("Network error. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (newPassword !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }
    if (newPassword.length < 8) {
      setError("Password must be at least 8 characters");
      return;
    }

    setLoading(true);
    try {
      const res = await fetch("/api/v1/auth/change-password", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...getCsrfHeaders(),
        },
        credentials: "include",
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
          new_email: email || undefined,
          remember_me: rememberMe,
        }),
      });

      if (!res.ok) {
        const data = await res.json();
        const authError = parseAuthError(data);
        setError(authError.message);
        return;
      }

      router.push("/workspace");
    } catch {
      setError("Network error. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  if (mode === "loading") {
    return (
      <PageShell>
        <p className="text-center text-sm text-[#6b7280]">Loading…</p>
      </PageShell>
    );
  }

  if (mode === "init_admin") {
    return (
      <PageShell>
        <BrandHeader />
        <div className="rounded-2xl border border-[#e5e7eb] bg-white p-8 shadow-sm">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-[#0a1628]">
              Create admin account
            </h1>
            <p className="mt-2 text-sm text-[#4b5563]">
              Set up the administrator account to get started.
            </p>
          </div>

          <form onSubmit={handleInitAdmin} className="mt-6 space-y-4">
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
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
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
                placeholder="At least 8 characters"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                required
                minLength={8}
                autoComplete="new-password"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label
                htmlFor="confirmPassword"
                className="text-xs font-medium tracking-wide text-[#374151] uppercase"
              >
                Confirm password
              </label>
              <Input
                id="confirmPassword"
                type="password"
                placeholder="Confirm password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                required
                minLength={8}
                autoComplete="new-password"
              />
            </div>
            <RememberSessionOption
              checked={rememberMe}
              onCheckedChange={setRememberMe}
            />
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
              {loading ? "Creating account…" : "Create Admin Account"}
            </Button>
          </form>
        </div>
      </PageShell>
    );
  }

  return (
    <PageShell>
      <BrandHeader />
      <div className="rounded-2xl border border-[#e5e7eb] bg-white p-8 shadow-sm">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-[#0a1628]">
            Complete account setup
          </h1>
          <p className="mt-2 text-sm text-[#4b5563]">
            Set your real email and a new password before continuing.
          </p>
        </div>

        <form onSubmit={handleChangePassword} className="mt-6 space-y-4">
          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="newEmail"
              className="text-xs font-medium tracking-wide text-[#374151] uppercase"
            >
              Email
            </label>
            <Input
              id="newEmail"
              type="email"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="email"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="currentPassword"
              className="text-xs font-medium tracking-wide text-[#374151] uppercase"
            >
              Current password
            </label>
            <Input
              id="currentPassword"
              type="password"
              placeholder="Current password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              required
              autoComplete="current-password"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="newPassword"
              className="text-xs font-medium tracking-wide text-[#374151] uppercase"
            >
              New password
            </label>
            <Input
              id="newPassword"
              type="password"
              placeholder="At least 8 characters"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              required
              minLength={8}
              autoComplete="new-password"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label
              htmlFor="confirmNewPassword"
              className="text-xs font-medium tracking-wide text-[#374151] uppercase"
            >
              Confirm new password
            </label>
            <Input
              id="confirmNewPassword"
              type="password"
              placeholder="Confirm new password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
              minLength={8}
              autoComplete="new-password"
            />
          </div>
          <RememberSessionOption
            checked={rememberMe}
            onCheckedChange={setRememberMe}
          />
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
            {loading ? "Setting up…" : "Complete Setup"}
          </Button>
        </form>
      </div>
    </PageShell>
  );
}
