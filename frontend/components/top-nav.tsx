"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { getDemoStatus, type DemoStatus } from "@/lib/api";
import { COPY } from "@/lib/copy";
import { Logo, Pill, cx } from "./ui";
import { TOP_BAR_SLOT_ID } from "./top-bar-slot";

function isCurrent(href: string, path: string): boolean {
  if (href === "/") return path === "/";
  return path === href || path.startsWith(`${href}/`);
}

function DemoBadge() {
  const [status, setStatus] = useState<DemoStatus | null>(null);
  useEffect(() => {
    let alive = true;
    getDemoStatus().then(
      (s) => alive && setStatus(s),
      () => alive && setStatus(null), // cannot load: show nothing; the server still enforces everything
    );
    return () => {
      alive = false;
    };
  }, []);
  if (status === null) return null;
  if (status.paused) return <Pill tone="ember-soft">{COPY.demo.paused}</Pill>;
  if (status.runs_left_today === null) return null;
  const left = status.runs_left_today;
  const [lead, ...rest] = COPY.demo.left(left).split(String(left));
  return (
    <span title={COPY.demo.resets} className="text-sm text-muted">
      {lead}
      <span className={cx("font-mono", left === 0 ? "text-ember" : "text-fg")}>{left}</span>
      {rest.join(String(left))}
    </span>
  );
}

export function TopNav() {
  const path = usePathname();
  return (
    <header className="sc-top">
      <div className="sc-top-in">
        <Link href="/" className="flex flex-none items-center gap-2.5 font-semibold text-white no-underline">
          <Logo />
          {COPY.brand}
        </Link>
        <nav className="sc-nav" aria-label="Main">
          {COPY.nav.map((item) => {
            const current = item.href === "/runs" ? isCurrent("/runs", path) : isCurrent(item.href, path);
            return (
              <Link key={item.href} href={item.href} aria-current={current ? "page" : undefined}>
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div id={TOP_BAR_SLOT_ID} className="ml-auto flex min-w-0 items-center gap-3 max-[960px]:order-2" />
        <DemoBadge />
      </div>
    </header>
  );
}
