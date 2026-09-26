import { forwardRef, type ButtonHTMLAttributes } from "react";

import { cn } from "@/lib/format";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "danger-outline";
type Size = "sm" | "md" | "icon";

const variants: Record<Variant, string> = {
  primary: "bg-zinc-900 text-white hover:bg-zinc-800 active:bg-zinc-950 disabled:bg-zinc-300",
  secondary:
    "bg-white text-zinc-800 border border-zinc-200 hover:bg-zinc-50 hover:border-zinc-300 active:bg-zinc-100 disabled:text-zinc-500",
  ghost: "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 disabled:text-zinc-300",
  danger: "bg-red-600 text-white hover:bg-red-700 active:bg-red-800 disabled:bg-red-300",
  "danger-outline":
    "bg-white text-red-700 border border-zinc-200 hover:bg-red-50 hover:border-red-200 disabled:text-red-300",
};

const sizes: Record<Size, string> = {
  sm: "h-8 px-3 text-[13px] gap-1.5",
  md: "h-9 px-3.5 text-sm gap-2",
  icon: "h-8 w-8 justify-center",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", className, type = "button", ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cn(
        "inline-flex shrink-0 items-center rounded-md font-medium transition-colors duration-150",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 focus-visible:ring-offset-2",
        "disabled:cursor-not-allowed [&_svg]:size-4 [&_svg]:shrink-0",
        variants[variant],
        sizes[size],
        className,
      )}
      {...props}
    />
  );
});

export function ButtonLink({
  href,
  children,
  className,
  variant = "secondary",
  size = "md",
  external,
}: {
  href: string;
  children: React.ReactNode;
  className?: string;
  variant?: Variant;
  size?: Size;
  external?: boolean;
}) {
  return (
    <a
      href={href}
      {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
      className={cn(
        "inline-flex shrink-0 items-center rounded-md font-medium transition-colors duration-150",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 focus-visible:ring-offset-2 [&_svg]:size-4",
        variants[variant],
        sizes[size],
        className,
      )}
    >
      {children}
    </a>
  );
}
