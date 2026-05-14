import clsx from "clsx";
import type { ReviewStatus } from "@/lib/types";

const COLORS: Record<string, string> = {
  queued: "bg-muted/20 text-muted",
  in_progress: "bg-accent/20 text-accent",
  agents_running: "bg-accent/20 text-accent",
  aggregating: "bg-accent/20 text-accent",
  posting: "bg-warn/20 text-warn",
  completed: "bg-ok/20 text-ok",
  escalated: "bg-warn/20 text-warn",
  failed: "bg-err/20 text-err",
};

export function ReviewStatusBadge({ status }: { status: ReviewStatus | string }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center px-2 py-0.5 rounded text-xs font-mono",
        COLORS[status] ?? "bg-panel text-muted"
      )}
    >
      {status}
    </span>
  );
}
