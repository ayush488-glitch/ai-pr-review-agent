import { SeverityChip } from "./SeverityChip";
import type { Finding } from "@/lib/types";

export function FindingCard({ finding }: { finding: Finding }) {
  return (
    <div className="px-4 py-3 space-y-1">
      <div className="flex items-start gap-2">
        <SeverityChip severity={finding.severity} />
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium">{finding.summary}</div>
          {finding.file_path && (
            <div className="text-xs font-mono text-muted truncate">
              {finding.file_path}
              {finding.line_start ? `:${finding.line_start}` : ""}
              {finding.line_end && finding.line_end !== finding.line_start
                ? `-${finding.line_end}`
                : ""}
            </div>
          )}
        </div>
        <span className="text-xs text-muted shrink-0">
          conf {(finding.confidence * 100).toFixed(0)}%
        </span>
      </div>
      {finding.suggestion && (
        <div className="mt-2 text-xs border-l-2 border-accent/40 pl-3 text-muted whitespace-pre-wrap">
          <span className="text-accent">suggestion: </span>
          {finding.suggestion}
        </div>
      )}
    </div>
  );
}

export function FindingsByAgent({ findings }: { findings: Finding[] }) {
  const groups = new Map<string, Finding[]>();
  for (const f of findings) {
    const k = f.agent_type || "unknown";
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k)!.push(f);
  }
  if (groups.size === 0) {
    return (
      <div className="border border-border rounded-lg bg-panel px-4 py-3 text-sm text-muted">
        No findings.
      </div>
    );
  }
  return (
    <div className="grid gap-4">
      {[...groups.entries()].map(([agent, list]) => (
        <div key={agent} className="border border-border rounded-lg bg-panel">
          <div className="flex items-center justify-between px-4 py-3 border-b border-border">
            <span className="font-mono text-sm text-accent uppercase">{agent}</span>
            <span className="text-xs text-muted">
              {list.length} finding{list.length === 1 ? "" : "s"}
            </span>
          </div>
          <div className="divide-y divide-border">
            {list.map((f) => (
              <FindingCard key={f.id} finding={f} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
