import type { HTMLAttributes, ReactNode } from "react";

import { cn } from "@/lib/format";

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("rounded-lg border border-zinc-200 bg-white shadow-card", className)} {...props} />;
}

export function CardHeader({
  title,
  description,
  action,
  icon,
  className,
  id,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <div className={cn("flex items-start justify-between gap-4 border-b border-zinc-100 px-5 py-3.5", className)}>
      <div className="flex min-w-0 items-start gap-2.5">
        {icon && <span className="mt-0.5 text-zinc-500 [&_svg]:size-4">{icon}</span>}
        <div className="min-w-0">
          <h2 id={id} className="text-[13px] font-semibold text-zinc-900">
            {title}
          </h2>
          {description && <p className="mt-0.5 text-[13px] text-zinc-500">{description}</p>}
        </div>
      </div>
      {action && <div className="flex shrink-0 items-center gap-2">{action}</div>}
    </div>
  );
}

export function CardBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-5 py-4", className)} {...props} />;
}

/** Small uppercase label used above values (MODEL, TOOLS, ...). */
export function Eyebrow({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("text-2xs font-medium uppercase tracking-[0.06em] text-zinc-500", className)}>{children}</div>;
}

export function KeyValue({ label, children, mono }: { label: ReactNode; children: ReactNode; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt>
        <Eyebrow>{label}</Eyebrow>
      </dt>
      <dd className={cn("mt-1 truncate text-sm text-zinc-900", mono && "font-mono text-[13px]")}>{children}</dd>
    </div>
  );
}
