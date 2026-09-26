// Status is never shown by color alone: every indicator pairs a shape/icon with text.
import { AlertTriangle, CheckCircle2, CircleDashed, Clock3, MinusCircle, XCircle } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/format";

export type Tone = "ok" | "warn" | "bad" | "neutral" | "info" | "pending";

const tones: Record<Tone, { chip: string; dot: string; icon: ReactNode }> = {
  ok: { chip: "bg-emerald-50 text-emerald-800 ring-emerald-600/20", dot: "bg-emerald-500", icon: <CheckCircle2 aria-hidden /> },
  warn: { chip: "bg-amber-50 text-amber-900 ring-amber-600/25", dot: "bg-amber-500", icon: <AlertTriangle aria-hidden /> },
  bad: { chip: "bg-red-50 text-red-800 ring-red-600/20", dot: "bg-red-500", icon: <XCircle aria-hidden /> },
  neutral: { chip: "bg-zinc-100 text-zinc-700 ring-zinc-500/15", dot: "bg-zinc-400", icon: <MinusCircle aria-hidden /> },
  info: { chip: "bg-accent-50 text-accent-800 ring-accent-600/20", dot: "bg-accent-500", icon: <CircleDashed aria-hidden /> },
  pending: { chip: "bg-amber-50 text-amber-900 ring-amber-600/25", dot: "bg-amber-500", icon: <Clock3 aria-hidden /> },
};

/** A pill with icon + label, e.g. "✓ Healthy". */
export function StatusBadge({ tone, children, className, icon = true }: { tone: Tone; children: ReactNode; className?: string; icon?: boolean }) {
  const t = tones[tone];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset [&_svg]:size-3.5",
        t.chip,
        className,
      )}
    >
      {icon && t.icon}
      {children}
    </span>
  );
}

/** A dot + text, for dense lists ("● Healthy"). The dot is decorative; the text carries the meaning. */
export function StatusDot({ tone, children, className }: { tone: Tone; children: ReactNode; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2 text-sm text-zinc-700", className)}>
      <span aria-hidden className={cn("size-2 rounded-full", tones[tone].dot, tone === "ok" && "ring-4 ring-emerald-500/15")} />
      {children}
    </span>
  );
}

export function StatusIcon({ tone, label, className }: { tone: Tone; label: string; className?: string }) {
  const color = { ok: "text-emerald-600", warn: "text-amber-600", bad: "text-red-600", neutral: "text-zinc-500", info: "text-accent-600", pending: "text-amber-600" }[tone];
  return (
    <span className={cn("inline-flex [&_svg]:size-4", color, className)} role="img" aria-label={label} title={label}>
      {tones[tone].icon}
    </span>
  );
}

export function Tag({ children, className, mono }: { children: ReactNode; className?: string; mono?: boolean }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border border-zinc-200 bg-zinc-50 px-1.5 py-px text-xs text-zinc-700",
        mono && "font-mono text-[11.5px]",
        className,
      )}
    >
      {children}
    </span>
  );
}
