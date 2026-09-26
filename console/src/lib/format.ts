import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

const ACRONYMS = new Set(["id", "url", "pii", "api", "ui", "ttl", "eta"]);

/** issue_refund -> "Issue refund", order_id -> "Order ID" */
export function humanize(name: string): string {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .split(/[_\-\s.]+/)
    .filter(Boolean)
    .map((w) => w.toLowerCase());
  return words
    .map((w, i) => (ACRONYMS.has(w) ? w.toUpperCase() : i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w))
    .join(" ");
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

/** agui-<uuid> -> first 8 characters of the uuid, for tables. */
export function shortId(id: string): string {
  const bare = id.replace(/^agui-/, "");
  return bare.length > 8 ? bare.slice(0, 8) : bare;
}

export function timeAgo(epochSeconds: number | null | undefined, now = Date.now()): string {
  if (!epochSeconds) return "—";
  const seconds = Math.round(now / 1000 - epochSeconds);
  if (seconds < 0) return timeUntil(epochSeconds, now);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

export function timeUntil(epochSeconds: number, now = Date.now()): string {
  const seconds = Math.round(epochSeconds - now / 1000);
  if (seconds <= 0) return "expired";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `in ${Math.max(1, minutes)} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `in ${hours} h`;
  return `in ${Math.round(hours / 24)} days`;
}

export function dateTime(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const m = Math.floor(seconds / 60);
  return `${m} min ${Math.round(seconds % 60)} s`;
}

export function number(n: number): string {
  return n.toLocaleString();
}

export function environmentLabel(env: string): string {
  return { local: "Local", dev: "Development", test: "Test", prod: "Production" }[env] ?? env;
}
