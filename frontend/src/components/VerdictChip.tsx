import clsx from "clsx";
import type { Verdict } from "@/lib/types";

const COLORS: Record<string, string> = {
  approve: "bg-ok/15 text-ok border-ok/30",
  request_changes: "bg-err/15 text-err border-err/30",
  dismiss: "bg-muted/15 text-muted border-muted/30",
  comment: "bg-accent/15 text-accent border-accent/30",
};

const LABELS: Record<string, string> = {
  approve: "Approve",
  request_changes: "Request changes",
  dismiss: "Dismiss",
  comment: "Comment",
};

export function VerdictChip({
  verdict,
  className,
}: {
  verdict?: Verdict | string | null;
  className?: string;
}) {
  if (!verdict) return null;
  const v = verdict as string;
  return (
    <span
      className={clsx(
        "inline-flex items-center px-2 py-0.5 rounded border text-xs font-medium",
        COLORS[v] ?? "bg-panel text-muted border-border",
        className
      )}
    >
      {LABELS[v] ?? v}
    </span>
  );
}
