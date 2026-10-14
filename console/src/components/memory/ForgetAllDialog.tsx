// Confirms "forget everything": irreversible, so it takes a deliberate second click.

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/overlay";

export function ForgetAllDialog({
  open,
  onOpenChange,
  count,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  count: number;
  onConfirm: () => Promise<boolean>;
}) {
  const [busy, setBusy] = useState(false);
  const confirm = async () => {
    setBusy(true);
    try {
      if (await onConfirm()) onOpenChange(false);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => !busy && onOpenChange(next)}
      title="Forget everything?"
      description={`The agent will forget all ${count} ${count === 1 ? "note" : "notes"} about you, in every conversation. This can't be undone.`}
      footer={
        <>
          <Button onClick={() => onOpenChange(false)} disabled={busy}>
            Keep them
          </Button>
          <Button variant="danger" onClick={confirm} disabled={busy}>
            {busy ? "Forgetting…" : "Forget everything"}
          </Button>
        </>
      }
    />
  );
}
