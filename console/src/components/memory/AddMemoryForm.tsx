// A note the person wants the agent to remember. Presentational: the page decides what "add" does.

import { Plus } from "lucide-react";
import { useId, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/format";

export function AddMemoryForm({
  maxChars,
  full,
  onAdd,
}: {
  maxChars: number;
  /** At the limit, a new note replaces the least recently used one; say so before it happens. */
  full: boolean;
  onAdd: (text: string) => Promise<boolean>;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const id = useId();
  const hint = useId();
  const trimmed = text.trim();
  const left = maxChars - text.length;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!trimmed || busy) return;
    setBusy(true);
    try {
      if (await onAdd(trimmed)) setText("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-2">
      <label htmlFor={id} className="block text-[13px] font-medium text-zinc-800">
        Add a note
      </label>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          id={id}
          value={text}
          maxLength={maxChars}
          placeholder="e.g. Prefers replies in bullet points"
          aria-describedby={hint}
          onChange={(e) => setText(e.target.value)}
          className="h-9 min-w-0 flex-1 rounded-md border border-zinc-300 px-3 text-sm text-zinc-900 placeholder:text-zinc-400 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/30"
        />
        <Button type="submit" variant="primary" disabled={!trimmed || busy}>
          <Plus aria-hidden /> {busy ? "Adding…" : "Remember"}
        </Button>
      </div>
      <p id={hint} className="flex flex-wrap justify-between gap-2 text-2xs text-zinc-500">
        <span>
          Preferences and working context only. Personal data (emails, phone and card numbers) and instructions to the agent are refused.
          {full && " The list is full: adding replaces the least recently updated note."}
        </span>
        <span className={cn("tabular-nums", left < 20 && "text-amber-700")} aria-live="polite">
          {left < 40 ? `${left} characters left` : ""}
        </span>
      </p>
    </form>
  );
}
