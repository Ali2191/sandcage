import type { ButtonHTMLAttributes, ReactNode } from "react";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

export function Eyebrow({ children, ember = false, className }: { children: ReactNode; ember?: boolean; className?: string }) {
  return (
    <div className={cx("text-xs font-semibold uppercase tracking-[0.12em]", ember ? "text-ember" : "text-muted", className)}>{children}</div>
  );
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cx("font-mono", className)}>{children}</span>;
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd aria-hidden="true">{children}</kbd>;
}

export function Card({ children, dashed = false, alert = false, className, as: Tag = "div", ...rest }: { children: ReactNode; dashed?: boolean; alert?: boolean; className?: string; as?: "div" | "section" | "li"; "aria-label"?: string; "aria-labelledby"?: string; id?: string }) {
  return (
    <Tag
      {...rest}
      className={cx(
        "rounded-card border p-5",
        dashed ? "border-dashed border-line-strong bg-transparent" : alert ? "border-ember-border bg-graphite" : "border-line bg-graphite",
        className,
      )}
    >
      {children}
    </Tag>
  );
}

export type PillTone = "ember" | "ember-soft" | "neutral" | "outline" | "solid";

const PILL: Record<PillTone, string> = {
  ember: "bg-ember text-on-ember font-semibold border-transparent",
  "ember-soft": "bg-ember-glow text-ember-soft border-ember-line",
  neutral: "border-line-strong text-body",
  outline: "border-line text-muted",
  solid: "bg-raised text-fg border-line-strong",
};

export function Pill({ tone = "outline", mono = false, children, className }: { tone?: PillTone; mono?: boolean; children: ReactNode; className?: string }) {
  return (
    <span
      className={cx(
        "inline-flex w-fit items-center rounded-full border px-2.5 py-0.5 text-xs leading-5",
        mono && "font-mono",
        PILL[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block size-4 animate-spin rounded-full border-2 border-current border-t-transparent motion-reduce:animate-none"
    />
  );
}

type ButtonKind = "main" | "secondary" | "ghost";
type ButtonSize = "lg" | "md" | "sm";

const BUTTON_KIND: Record<ButtonKind, string> = {
  main: "sc-btn-main border-0 bg-white text-black hover:bg-[#e6e6e6]",
  secondary: "border border-line-strong bg-transparent text-fg hover:border-fg",
  ghost: "border border-transparent bg-transparent text-muted hover:text-fg",
};
const BUTTON_SIZE: Record<ButtonSize, string> = {
  lg: "h-12 px-6 text-[15px]",
  md: "h-11 px-5 text-[15px]",
  sm: "h-8 px-3.5 text-[13px]",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  kind?: ButtonKind;
  size?: ButtonSize;
  busy?: boolean;
}

export function Button({ kind = "secondary", size = "md", busy = false, disabled, className, children, type = "button", ...rest }: ButtonProps) {
  return (
    <button
      {...rest}
      type={type}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={cx(
        "inline-flex cursor-pointer items-center justify-center gap-2.5 rounded-full font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        BUTTON_KIND[kind],
        BUTTON_SIZE[size],
        className,
      )}
    >
      {busy && <Spinner />}
      {children}
    </button>
  );
}

export function Skeleton({ className, label }: { className?: string; label?: string }) {
  return <div role={label ? "status" : undefined} aria-label={label} className={cx("animate-pulse-soft rounded-xl bg-raised", className)} />;
}

export function Empty({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="rounded-card border border-dashed border-line-strong px-6 py-8 text-[15px] text-muted">
      <p className="m-0 max-w-[60ch]">{children}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorCard({ title, children, onRetry }: { title?: string; children: ReactNode; onRetry?: () => void }) {
  return (
    <Card alert className="text-ember" >
      <div role="alert">
        {title && <div className="mb-1 font-semibold text-ember">{title}</div>}
        <div className="text-[15px] text-ember">{children}</div>
        {onRetry && (
          <Button size="sm" className="mt-3" onClick={onRetry}>
            Retry
          </Button>
        )}
      </div>
    </Card>
  );
}

export function Facts({ rows }: { rows: { label: string; value: string; mono?: boolean }[] }) {
  return (
    <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
      {rows.map((r) => (
        <div key={r.label} className="contents">
          <dt className="text-muted">{r.label}</dt>
          <dd className={cx("m-0 min-w-0 break-words text-fg", r.mono && "font-mono")}>{r.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Meter({ label, value, fraction, tone = "light" }: { label: string; value: string; fraction: number | null; tone?: "light" | "ember" }) {
  return (
    <div>
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className="text-muted">{label}</span>
        <span className={cx("font-mono tabular-nums", tone === "ember" ? "text-ember" : "text-fg")}>{value}</span>
      </div>
      {fraction !== null && (
        <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-raised" role="presentation">
          <div className={cx("h-full rounded-full", tone === "ember" ? "bg-ember" : "bg-body")} style={{ width: `${Math.round(Math.min(1, Math.max(0, fraction)) * 100)}%` }} />
        </div>
      )}
    </div>
  );
}

export function CheckIcon({ className }: { className?: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" className={cx("size-5 shrink-0", className)} fill="none" stroke="currentColor" strokeWidth="1.6">
      <circle cx="10" cy="10" r="8" />
      <path d="m6.5 10.2 2.4 2.4 4.6-4.9" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** The blocked mark: a circle with a slash. Blocked is never colour alone. */
export function BlockedIcon({ className }: { className?: string }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" className={cx("size-5 shrink-0", className)} fill="none" stroke="currentColor" strokeWidth="1.6">
      <circle cx="10" cy="10" r="8" />
      <path d="m4.6 15.4 10.8-10.8" strokeLinecap="round" />
    </svg>
  );
}

export function Logo() {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" className="size-6" fill="none" stroke="currentColor" strokeWidth="1.7">
      <rect x="3.5" y="3.5" width="17" height="17" rx="3" />
      <path d="M9.2 3.5v17M14.8 3.5v17" />
    </svg>
  );
}
