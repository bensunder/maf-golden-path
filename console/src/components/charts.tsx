// Small, dependency-free SVG charts for time series. One series per chart (the card title names it), a
// crosshair + tooltip on hover and keyboard focus, and a table view so no value needs the pointer.
import { useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";

import { cn } from "@/lib/format";

export interface Point {
  t: number; // epoch ms
  v: number | null;
  partial?: boolean; // an interval only partly inside the range, or still filling in
}

const H = 160;
const PAD = { top: 12, right: 12, bottom: 22, left: 44 };

function niceMax(max: number): number {
  if (max <= 0) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(max)));
  const f = max / exp;
  const nice = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].find((n) => f <= n) ?? 10;
  return nice * exp;
}

export function compact(n: number): string {
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}k`;
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(n < 10 ? 2 : 1)));
}

function timeLabel(t: number, binMinutes: number): string {
  const d = new Date(t);
  if (binMinutes >= 360) return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric" });
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: binMinutes < 60 ? "2-digit" : undefined });
}

export function TimeChart({
  points,
  kind,
  binMinutes,
  format = compact,
  label,
  showTable,
}: {
  points: Point[];
  kind: "bar" | "line";
  binMinutes: number;
  format?: (v: number) => string;
  label: string; // what the value is, e.g. "Runs"
  showTable?: boolean;
}) {
  const [width, setWidth] = useState(320);
  const [hover, setHover] = useState<number | null>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const id = useId();

  // Measure on mount and resize, so the SVG uses real pixels (crisp 2px lines, true 4px radii).
  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    setWidth(Math.max(240, Math.round(el.clientWidth)));
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.max(240, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const { max, x, y, step } = useMemo(() => {
    const vals = points.map((p) => p.v ?? 0);
    const m = niceMax(Math.max(0, ...vals));
    const innerW = width - PAD.left - PAD.right;
    const n = Math.max(points.length, 1);
    const st = innerW / n;
    return {
      max: m,
      step: st,
      x: (i: number) => PAD.left + st * i + st / 2,
      y: (v: number) => PAD.top + (H - PAD.top - PAD.bottom) * (1 - v / m),
    };
  }, [points, width]);

  const ticks = [0, max / 2, max];
  const baseline = y(0);
  const labelEvery = Math.max(1, Math.ceil(points.length / Math.max(2, Math.floor(width / 90))));

  const onMove = (e: PointerEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const i = Math.floor((e.clientX - rect.left - PAD.left) / step);
    setHover(i >= 0 && i < points.length ? i : null);
  };
  const onKey = (e: KeyboardEvent<SVGSVGElement>) => {
    if (!points.length) return;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      setHover((h) => {
        const cur = h ?? (e.key === "ArrowRight" ? -1 : points.length);
        return Math.min(points.length - 1, Math.max(0, cur + (e.key === "ArrowRight" ? 1 : -1)));
      });
    } else if (e.key === "Escape") setHover(null);
  };

  const linePath = points
    .map((p, i) => (p.v === null ? null : `${x(i)},${y(p.v)}`))
    .reduce<string[]>((acc, pt, i) => {
      if (pt === null) return acc;
      const prevNull = i === 0 || points[i - 1].v === null;
      acc.push(`${prevNull ? "M" : "L"}${pt}`);
      return acc;
    }, [])
    .join(" ");

  const hovered = hover !== null ? points[hover] : null;
  const barW = Math.max(2, Math.min(28, step - 2)); // 2px surface gap between bars

  return (
    <div ref={wrap} className="relative w-full min-w-0">
      <svg
        width={width}
        height={H}
        role="img"
        aria-label={`${label} over time. Use the arrow keys to read values.`}
        aria-describedby={`${id}-live`}
        tabIndex={0}
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
        onKeyDown={onKey}
        onBlur={() => setHover(null)}
        className="block rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500"
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD.left} x2={width - PAD.right} y1={y(t)} y2={y(t)} stroke="#e4e4e7" strokeWidth={1} strokeDasharray={t === 0 ? undefined : "2 3"} />
            <text x={PAD.left - 8} y={y(t)} dy="0.32em" textAnchor="end" className="fill-zinc-500 text-[11px] tabular-nums">
              {format(t)}
            </text>
          </g>
        ))}
        {points.map((p, i) =>
          i % labelEvery === 0 ? (
            <text key={p.t} x={x(i)} y={H - 6} textAnchor="middle" className="fill-zinc-500 text-[11px]">
              {timeLabel(p.t, binMinutes)}
            </text>
          ) : null,
        )}
        {hover !== null && <line x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={baseline} stroke="#a1a1aa" strokeWidth={1} />}
        {kind === "bar"
          ? points.map((p, i) => {
              const v = p.v ?? 0;
              if (v <= 0) return null;
              const top = y(v);
              const h = Math.max(1, baseline - top);
              const r = Math.min(4, h, barW / 2);
              const x0 = x(i) - barW / 2;
              // rounded data end, square at the baseline
              const d = `M${x0},${baseline} V${top + r} Q${x0},${top} ${x0 + r},${top} H${x0 + barW - r} Q${x0 + barW},${top} ${x0 + barW},${top + r} V${baseline} Z`;
              const base = p.partial ? 0.4 : 1;
              return <path key={p.t} d={d} fill="#3b6ef6" opacity={hover === null || hover === i ? base : base * 0.55} />;
            })
          : (
              <>
                <path d={linePath} fill="none" stroke="#3b6ef6" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
                {hovered && hovered.v !== null && hover !== null && (
                  <circle cx={x(hover)} cy={y(hovered.v)} r={4} fill="#3b6ef6" stroke="#fff" strokeWidth={2} />
                )}
              </>
            )}
      </svg>
      {hovered && hover !== null && (
        <div
          className="pointer-events-none absolute top-1 z-10 rounded-md border border-zinc-200 bg-white px-2.5 py-1.5 text-xs shadow-pop"
          style={{ left: Math.min(Math.max(x(hover) - 60, 0), width - 130) }}
        >
          <div className="font-semibold tabular-nums text-zinc-950">{hovered.v === null ? "No data" : format(hovered.v)}</div>
          <div className="text-zinc-500">{new Date(hovered.t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</div>
          {hovered.partial && <div className="text-zinc-500">Partial interval</div>}
        </div>
      )}
      <span id={`${id}-live`} className="sr-only" aria-live="polite">
        {hovered ? `${label} ${hovered.v === null ? "no data" : format(hovered.v)} at ${new Date(hovered.t).toLocaleString()}` : ""}
      </span>
      {showTable && (
        <div className="mt-3 max-h-56 overflow-y-auto rounded-md border border-zinc-200">
          <table className="w-full text-xs" aria-label={`${label} by time`}>
            <thead className="sticky top-0 bg-zinc-50">
              <tr>
                <th scope="col" className="px-3 py-1.5 text-left font-medium text-zinc-500">Time</th>
                <th scope="col" className="px-3 py-1.5 text-right font-medium text-zinc-500">{label}</th>
              </tr>
            </thead>
            <tbody>
              {points.map((p) => (
                <tr key={p.t} className="border-t border-zinc-100">
                  <td className="px-3 py-1 text-zinc-700">{new Date(p.t).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" })}</td>
                  <td className="px-3 py-1 text-right tabular-nums text-zinc-900">{p.v === null ? "—" : format(p.v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** Every bin the query can return, oldest first: missing ones are quiet periods (zero, or a gap for averages).
 *  ``ago(range)`` starts inside a bin, so there is one more bin than range/bin; the first and the current
 *  bins cover only part of their interval (``partial``). KQL bin() and these bins both align to UTC. */
export function fillBins<T extends { t: string | number }>(
  rows: T[],
  binMinutes: number,
  rangeMinutes: number,
  now = Date.now(),
): (T | null)[] & { times?: number[]; partial?: boolean[] } {
  const step = binMinutes * 60_000;
  const end = Math.floor(now / step) * step;
  const start = Math.floor((now - rangeMinutes * 60_000) / step) * step;
  const byT = new Map(rows.map((r) => [Math.floor(new Date(r.t).getTime() / step) * step, r]));
  const out: (T | null)[] & { times?: number[]; partial?: boolean[] } = [];
  out.times = [];
  out.partial = [];
  for (let t = start; t <= end; t += step) {
    out.push(byT.get(t) ?? null);
    out.times.push(t);
    out.partial.push(t === start || t === end);
  }
  return out;
}

export function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string }) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-md border border-zinc-200 bg-white p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            "rounded px-2.5 py-1 text-[13px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500",
            value === o.value ? "bg-zinc-900 font-medium text-white" : "text-zinc-600 hover:bg-zinc-100",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
