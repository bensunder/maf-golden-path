import type { HTMLAttributes, ReactNode, TdHTMLAttributes, ThHTMLAttributes } from "react";

import { cn } from "@/lib/format";

export function Table({ children, className, label }: { children: ReactNode; className?: string; label?: string }) {
  return (
    <div className={cn("w-full overflow-x-auto", className)}>
      <table className="w-full border-collapse text-sm" aria-label={label}>
        {children}
      </table>
    </div>
  );
}

export function Th({ className, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      scope="col"
      className={cn(
        "whitespace-nowrap border-b border-zinc-200 bg-zinc-50/60 px-4 py-2 text-left text-2xs font-medium uppercase tracking-[0.06em] text-zinc-500 first:pl-5 last:pr-5",
        className,
      )}
      {...props}
    />
  );
}

export function Td({ className, ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn("border-b border-zinc-100 px-4 py-2.5 align-middle text-zinc-700 first:pl-5 last:pr-5", className)} {...props} />;
}

export function Tr({ className, interactive, ...props }: HTMLAttributes<HTMLTableRowElement> & { interactive?: boolean }) {
  return <tr className={cn("[&:last-child>td]:border-b-0", interactive && "cursor-pointer transition-colors hover:bg-zinc-50", className)} {...props} />;
}
