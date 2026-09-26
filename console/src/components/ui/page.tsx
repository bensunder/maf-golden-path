import type { ReactNode } from "react";

import { cn } from "@/lib/format";

export function Page({ children, className, wide }: { children: ReactNode; className?: string; wide?: boolean }) {
  return <div className={cn("mx-auto w-full px-4 pb-16 pt-6 sm:px-6 lg:px-8", wide ? "max-w-[1400px]" : "max-w-[1200px]", className)}>{children}</div>;
}

export function PageHeader({
  title,
  description,
  actions,
  meta,
  eyebrow,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  meta?: ReactNode;
  eyebrow?: ReactNode;
}) {
  return (
    <header className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        {eyebrow && <div className="mb-1.5">{eyebrow}</div>}
        <h1 className="text-xl font-semibold tracking-[-0.01em] text-zinc-950">{title}</h1>
        {description && <p className="mt-1 text-sm text-zinc-500">{description}</p>}
        {meta && <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">{meta}</div>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

export function SectionTitle({ children, action, id }: { children: ReactNode; action?: ReactNode; id?: string }) {
  return (
    <div className="mb-3 mt-8 flex items-center justify-between gap-3 first:mt-0">
      <h2 id={id} className="text-[13px] font-semibold uppercase tracking-[0.06em] text-zinc-500">
        {children}
      </h2>
      {action}
    </div>
  );
}

export function Divider({ className }: { className?: string }) {
  return <hr className={cn("border-zinc-100", className)} />;
}
