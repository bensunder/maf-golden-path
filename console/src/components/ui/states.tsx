import { AlertOctagon, RotateCw } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/format";

import { Button } from "./button";

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn(
        "relative overflow-hidden rounded bg-zinc-100",
        "before:absolute before:inset-0 before:-translate-x-full before:animate-[shimmer_1.6s_infinite] before:bg-gradient-to-r before:from-transparent before:via-white/70 before:to-transparent",
        "motion-reduce:before:hidden",
        className,
      )}
    />
  );
}

/** Screen readers hear one "Loading" instead of a pile of empty boxes. */
export function LoadingRegion({ label = "Loading", children, className }: { label?: string; children: ReactNode; className?: string }) {
  return (
    <div role="status" aria-live="polite" aria-busy="true" className={className}>
      <span className="sr-only">{label}…</span>
      {children}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  children,
  action,
  className,
  compact,
}: {
  icon?: ReactNode;
  title: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
  compact?: boolean;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center text-center", compact ? "px-6 py-8" : "px-6 py-14", className)}>
      {icon && (
        <div className="mb-3 flex size-10 items-center justify-center rounded-lg border border-zinc-200 bg-zinc-50 text-zinc-500 [&_svg]:size-5">
          {icon}
        </div>
      )}
      <h2 className="text-sm font-semibold text-zinc-900">{title}</h2>
      {children && <div className="mt-1 max-w-md text-[13px] leading-relaxed text-zinc-500">{children}</div>}
      {action && <div className="mt-4 flex flex-wrap justify-center gap-2">{action}</div>}
    </div>
  );
}

export function ErrorState({
  title = "Something went wrong",
  message,
  onRetry,
  className,
  compact,
}: {
  title?: string;
  message: string;
  onRetry?: () => void;
  className?: string;
  compact?: boolean;
}) {
  return (
    <div role="alert" className={cn("flex flex-col items-center justify-center text-center", compact ? "px-6 py-8" : "px-6 py-14", className)}>
      <div className="mb-3 flex size-10 items-center justify-center rounded-lg border border-red-100 bg-red-50 text-red-600 [&_svg]:size-5">
        <AlertOctagon aria-hidden />
      </div>
      <h2 className="text-sm font-semibold text-zinc-900">{title}</h2>
      <p className="mt-1 max-w-md text-[13px] leading-relaxed text-zinc-500">{message}</p>
      {onRetry && (
        <Button size="sm" className="mt-4" onClick={onRetry}>
          <RotateCw aria-hidden /> Retry
        </Button>
      )}
    </div>
  );
}

export function InlineNotice({ tone = "info", children, icon, className }: { tone?: "info" | "warn" | "bad"; children: ReactNode; icon?: ReactNode; className?: string }) {
  const styles = {
    info: "border-zinc-200 bg-zinc-50 text-zinc-700",
    warn: "border-amber-200 bg-amber-50 text-amber-900",
    bad: "border-red-200 bg-red-50 text-red-800",
  }[tone];
  return (
    <div className={cn("flex items-start gap-2.5 rounded-md border px-3.5 py-2.5 text-[13px] leading-relaxed [&_svg]:mt-0.5 [&_svg]:size-4 [&_svg]:shrink-0", styles, className)}>
      {icon}
      <div className="min-w-0">{children}</div>
    </div>
  );
}
