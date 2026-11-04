import type { ReactNode } from "react";
import { cx } from "./ui";
import { TopNav } from "./top-nav";
import { ShortcutLegend } from "./shortcut-legend";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="sc-app text-fg">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-2 focus:z-50 focus:rounded-full focus:bg-white focus:px-4 focus:py-2 focus:text-black">
        Skip to the page
      </a>
      <TopNav />
      {children}
      <ShortcutLegend />
    </div>
  );
}

/** Two columns on desktop (main and a 380px side panel, each scrolling), one under 960px. */
export function Body({ children }: { children: ReactNode }) {
  return <div className="sc-body">{children}</div>;
}

export function Main({ children, flush = false, id = "main", className }: { children: ReactNode; flush?: boolean; id?: string; className?: string }) {
  return (
    <main id={id} className={cx("sc-main", className)} tabIndex={-1}>
      {flush ? children : <div className="mx-auto max-w-[920px] px-10 pb-12 pt-10 max-[960px]:px-4 max-[960px]:pb-10 max-[960px]:pt-7">{children}</div>}
    </main>
  );
}

export function Side({ children, label }: { children: ReactNode; label: string }) {
  return (
    <aside className="sc-aside" aria-label={label}>
      {children}
    </aside>
  );
}

/** A single centred column with no side panel (Runs, Bench, Policy, errors). */
export function Centered({ children, max = 820 }: { children: ReactNode; max?: number }) {
  return (
    <main id="main" className="sc-main flex-1" tabIndex={-1}>
      <div className="sc-center" style={{ maxWidth: max + 48 }}>
        {children}
      </div>
    </main>
  );
}
