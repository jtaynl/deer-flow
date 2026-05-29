"use client";

import { MessageSquarePlus } from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";

import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { useI18n } from "@/core/i18n/hooks";
import { env } from "@/env";
import { cn } from "@/lib/utils";

function WriLogo({ size = 28, className }: { size?: number; className?: string }) {
  return (
    <Image
      src="/wri/android-chrome-192x192.png"
      alt="WRI AI"
      width={size}
      height={size}
      className={cn("rounded-sm", className)}
      priority
    />
  );
}

function WriBrand({ asLink }: { asLink: boolean }) {
  const inner = (
    <>
      <WriLogo />
      <div className="flex flex-col leading-tight">
        <span className="text-sm font-semibold tracking-tight">WRI AI</span>
        <span className="text-[9px] uppercase tracking-wider text-[#7b1e2b]">
          World Research Institute
        </span>
      </div>
    </>
  );
  if (asLink) {
    return (
      <Link
        href="/"
        className="ml-2 flex items-center gap-2 text-foreground transition-opacity hover:opacity-80"
      >
        {inner}
      </Link>
    );
  }
  return (
    <div className="ml-2 flex items-center gap-2 text-foreground cursor-default">
      {inner}
    </div>
  );
}

export function WorkspaceHeader({ className }: { className?: string }) {
  const { t } = useI18n();
  const { state } = useSidebar();
  const pathname = usePathname();
  return (
    <>
      <div
        className={cn(
          "group/workspace-header flex h-12 flex-col justify-center",
          className,
        )}
      >
        {state === "collapsed" ? (
          <div className="group-has-data-[collapsible=icon]/sidebar-wrapper:-translate-y flex w-full cursor-pointer items-center justify-center">
            <div className="block pt-1 group-hover/workspace-header:hidden">
              <WriLogo size={24} />
            </div>
            <SidebarTrigger className="hidden pl-2 group-hover/workspace-header:block" />
          </div>
        ) : (
          <div className="flex items-center justify-between gap-2">
            <WriBrand asLink={env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === "true"} />
            <SidebarTrigger />
          </div>
        )}
      </div>
      <SidebarMenu>
        <SidebarMenuItem>
          <SidebarMenuButton
            isActive={pathname === "/workspace/chats/new"}
            asChild
          >
            <Link className="text-muted-foreground" href="/workspace/chats/new">
              <MessageSquarePlus size={16} />
              <span>{t.sidebar.newChat}</span>
            </Link>
          </SidebarMenuButton>
        </SidebarMenuItem>
      </SidebarMenu>
    </>
  );
}
