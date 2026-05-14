import clsx from "clsx";

const COLORS: Record<string, string> = {
  info: "bg-muted/20 text-muted",
  low: "bg-accent/15 text-accent",
  medium: "bg-warn/15 text-warn",
  high: "bg-warn/25 text-warn",
  critical: "bg-err/20 text-err",
};

export function SeverityChip({ severity }: { severity: string }) {
  const k = (severity ?? "").toLowerCase();
  return (
    <span
      className={clsx(
        "inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-mono uppercase tracking-wide",
        COLORS[k] ?? "bg-panel text-muted"
      )}
    >
      {severity}
    </span>
  );
}
