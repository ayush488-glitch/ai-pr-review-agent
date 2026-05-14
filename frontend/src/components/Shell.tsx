"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import clsx from "clsx";

const NAV = [
  { href: "/", label: "Dashboard" },
  { href: "/reviews", label: "Reviews" },
  { href: "/hitl", label: "HITL Queue" },
  { href: "/economics", label: "Economics" },
  { href: "/health", label: "Health" },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  return (
    <div className="min-h-screen flex">
      <aside className="w-56 border-r border-border bg-panel flex flex-col">
        <div className="px-4 py-5 border-b border-border">
          <div className="font-mono text-sm text-accent">ai-pr-review</div>
          <div className="text-[10px] text-muted uppercase tracking-wider mt-1">
            agent dashboard
          </div>
        </div>
        <nav className="flex-1 px-2 py-3 space-y-0.5">
          {NAV.map((item) => {
            const active =
              pathname === item.href ||
              (item.href !== "/" && pathname?.startsWith(item.href));
            return (
              <Link
                key={item.href}
                href={item.href}
                className={clsx(
                  "block px-3 py-2 rounded text-sm transition",
                  active
                    ? "bg-accent/15 text-accent"
                    : "text-muted hover:text-white hover:bg-bg"
                )}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="px-4 py-3 border-t border-border text-[10px] text-muted font-mono">
          phase 2 · polling
        </div>
      </aside>
      <main className="flex-1 px-8 py-6 overflow-x-hidden">{children}</main>
    </div>
  );
}
