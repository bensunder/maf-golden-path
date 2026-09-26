import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as Menu from "@radix-ui/react-dropdown-menu";
import { Check, Copy, X } from "lucide-react";
import { useState, type ReactNode } from "react";

import { cn } from "@/lib/format";

import { Button } from "./button";

// ------------------------------------------------------------------ dialog
export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  className,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  className?: string;
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-zinc-950/30 animate-fade-in motion-reduce:animate-none" />
        <DialogPrimitive.Content
          className={cn(
            "fixed left-1/2 top-1/2 z-50 w-[calc(100vw-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 rounded-xl border border-zinc-200 bg-white shadow-pop",
            "animate-scale-in focus:outline-none motion-reduce:animate-none",
            className,
          )}
        >
          <div className="flex items-start justify-between gap-4 border-b border-zinc-100 px-5 py-4">
            <div>
              <DialogPrimitive.Title className="text-[15px] font-semibold text-zinc-950">{title}</DialogPrimitive.Title>
              {description ? (
                <DialogPrimitive.Description className="mt-1 text-[13px] text-zinc-500">{description}</DialogPrimitive.Description>
              ) : (
                <DialogPrimitive.Description className="sr-only">Dialog</DialogPrimitive.Description>
              )}
            </div>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon" aria-label="Close">
                <X aria-hidden />
              </Button>
            </DialogPrimitive.Close>
          </div>
          {children && <div className="px-5 py-4">{children}</div>}
          {footer && <div className="flex flex-wrap justify-end gap-2 border-t border-zinc-100 bg-zinc-50/50 px-5 py-3">{footer}</div>}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/** A right-hand panel for details (session inspector). */
export function Sheet({
  open,
  onOpenChange,
  title,
  description,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-zinc-950/20 animate-fade-in motion-reduce:animate-none" />
        <DialogPrimitive.Content className="fixed inset-y-0 right-0 z-50 flex w-full max-w-xl flex-col border-l border-zinc-200 bg-white shadow-pop animate-slide-in focus:outline-none motion-reduce:animate-none">
          <div className="flex items-start justify-between gap-4 border-b border-zinc-100 px-5 py-4">
            <div className="min-w-0">
              <DialogPrimitive.Title className="truncate text-[15px] font-semibold text-zinc-950">{title}</DialogPrimitive.Title>
              <DialogPrimitive.Description className={description ? "mt-1 text-[13px] text-zinc-500" : "sr-only"}>
                {description ?? "Details"}
              </DialogPrimitive.Description>
            </div>
            <DialogPrimitive.Close asChild>
              <Button variant="ghost" size="icon" aria-label="Close">
                <X aria-hidden />
              </Button>
            </DialogPrimitive.Close>
          </div>
          <div className="flex-1 overflow-y-auto">{children}</div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

// ------------------------------------------------------------------ menu
export const MenuRoot = Menu.Root;
export const MenuTrigger = Menu.Trigger;

export function MenuContent({ children, align = "end", className }: { children: ReactNode; align?: "start" | "end"; className?: string }) {
  return (
    <Menu.Portal>
      <Menu.Content
        align={align}
        sideOffset={6}
        className={cn("z-50 min-w-[220px] rounded-lg border border-zinc-200 bg-white p-1 shadow-pop animate-fade-in motion-reduce:animate-none", className)}
      >
        {children}
      </Menu.Content>
    </Menu.Portal>
  );
}

export function MenuItem({ children, onSelect, asChild }: { children: ReactNode; onSelect?: () => void; asChild?: boolean }) {
  return (
    <Menu.Item
      asChild={asChild}
      onSelect={onSelect}
      className="flex cursor-pointer select-none items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] text-zinc-700 outline-none data-[highlighted]:bg-zinc-100 data-[highlighted]:text-zinc-900 [&_svg]:size-4 [&_svg]:text-zinc-500"
    >
      {children}
    </Menu.Item>
  );
}

export function MenuLabel({ children }: { children: ReactNode }) {
  return <Menu.Label className="px-2.5 py-1.5 text-xs text-zinc-500">{children}</Menu.Label>;
}

export function MenuSeparator() {
  return <Menu.Separator className="my-1 h-px bg-zinc-100" />;
}

// ------------------------------------------------------------------ copy
export function CopyButton({ value, label = "Copy", className }: { value: string; label?: string; className?: string }) {
  const [done, setDone] = useState(false);
  return (
    <Button
      variant="ghost"
      size="icon"
      className={cn("h-7 w-7", className)}
      aria-label={done ? "Copied" : label}
      title={done ? "Copied" : label}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(value);
          setDone(true);
          window.setTimeout(() => setDone(false), 1500);
        } catch {
          /* clipboard blocked */
        }
      }}
    >
      {done ? <Check aria-hidden className="text-emerald-600" /> : <Copy aria-hidden />}
    </Button>
  );
}
