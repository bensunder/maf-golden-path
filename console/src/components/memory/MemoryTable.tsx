// What the agent remembers about the signed-in person, one row per note, with inline edit and delete.
// Presentational only: every change is reported through callbacks; the page owns the state.

import { Check, Pencil, Trash2, X } from "lucide-react";
import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { Button } from "@/components/ui/button";
import { Tag } from "@/components/ui/status";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { dateTime, timeAgo, timeUntil } from "@/lib/format";
import type { MemoryItem } from "@/lib/memory";

export interface MemoryTableProps {
  items: MemoryItem[];
  maxChars: number;
  /** Ids with a change still waiting for the service (shown as saving, actions disabled). */
  pending: ReadonlySet<string>;
  onEdit: (id: string, text: string) => Promise<boolean>;
  onRemove: (id: string) => void;
}

export function MemoryTable({ items, maxChars, pending, onEdit, onRemove }: MemoryTableProps) {
  const [editing, setEditing] = useState<string | null>(null);
  return (
    <Table label="Remembered notes">
      <thead>
        <tr>
          <Th>Note</Th>
          <Th className="hidden md:table-cell">Added by</Th>
          <Th className="hidden sm:table-cell">Updated</Th>
          <Th className="hidden lg:table-cell">Forgotten</Th>
          <Th>
            <span className="sr-only">Actions</span>
          </Th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <Tr key={item.id} aria-busy={pending.has(item.id) || undefined}>
            <Td className="w-full max-w-0">
              {editing === item.id ? (
                <EditField
                  item={item}
                  maxChars={maxChars}
                  onCancel={() => setEditing(null)}
                  onSave={async (text) => {
                    setEditing(null);
                    if (text !== item.text) await onEdit(item.id, text);
                  }}
                />
              ) : (
                <div className="min-w-0">
                  <p className="break-words text-zinc-900">{item.text}</p>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5">
                    <Tag>{item.kind === "fact" ? "Fact" : "Preference"}</Tag>
                    <span className="text-2xs text-zinc-500 md:hidden">{item.source === "user" ? "Added by you" : "Added by the agent"}</span>
                    {pending.has(item.id) && <span className="text-2xs text-zinc-500">Saving…</span>}
                  </div>
                </div>
              )}
            </Td>
            <Td className="hidden whitespace-nowrap md:table-cell">{item.source === "user" ? "You" : "The agent"}</Td>
            <Td className="hidden whitespace-nowrap sm:table-cell">
              <time dateTime={new Date(item.updated_at * 1000).toISOString()} title={dateTime(item.updated_at)}>
                {timeAgo(item.updated_at)}
              </time>
            </Td>
            <Td className="hidden whitespace-nowrap lg:table-cell">
              <time dateTime={new Date(item.expires_at * 1000).toISOString()} title={dateTime(item.expires_at)}>
                {timeUntil(item.expires_at)}
              </time>
            </Td>
            <Td className="whitespace-nowrap text-right">
              {editing !== item.id && (
                <div className="flex justify-end gap-1">
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Edit: ${item.text}`}
                    disabled={pending.has(item.id)}
                    onClick={() => setEditing(item.id)}
                  >
                    <Pencil aria-hidden />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Forget: ${item.text}`}
                    disabled={pending.has(item.id)}
                    onClick={() => onRemove(item.id)}
                  >
                    <Trash2 aria-hidden />
                  </Button>
                </div>
              )}
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

function EditField({
  item,
  maxChars,
  onSave,
  onCancel,
}: {
  item: MemoryItem;
  maxChars: number;
  onSave: (text: string) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState(item.text);
  const input = useRef<HTMLInputElement>(null);
  const id = useId();
  useEffect(() => input.current?.select(), []);

  const trimmed = text.trim();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (trimmed) onSave(trimmed);
  };
  const keys = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onCancel();
    }
  };

  return (
    <form onSubmit={submit} className="flex items-center gap-1.5">
      <label htmlFor={id} className="sr-only">
        Edit note
      </label>
      <input
        id={id}
        ref={input}
        value={text}
        maxLength={maxChars}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={keys}
        className="h-8 min-w-0 flex-1 rounded-md border border-zinc-300 px-2.5 text-sm text-zinc-900 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/30"
      />
      <Button type="submit" variant="ghost" size="icon" aria-label="Save" disabled={!trimmed}>
        <Check aria-hidden />
      </Button>
      <Button variant="ghost" size="icon" aria-label="Cancel" onClick={onCancel}>
        <X aria-hidden />
      </Button>
    </form>
  );
}
