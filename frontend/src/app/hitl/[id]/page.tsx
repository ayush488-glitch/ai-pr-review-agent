"use client";

import useSWR from "swr";
import { useParams } from "next/navigation";
import { FindingsByAgent } from "@/components/AgentFindingCard";
import { HITLDecisionForm } from "@/components/HITLDecisionForm";
import { VerdictChip } from "@/components/VerdictChip";
import { Empty } from "@/components/Empty";
import type { HITLDetail } from "@/lib/types";

export default function HITLDetailPage() {
  const params = useParams<{ id: string }>();
  const id = decodeURIComponent(params.id);
  const { data, error, isLoading } = useSWR<HITLDetail>(
    `/api/v1/hitl/${encodeURIComponent(id)}`
  );

  if (error) return <Empty>Failed to load: {error.message}</Empty>;
  if (isLoading || !data) return <Empty>Loading…</Empty>;

  const prUrl = `https://github.com/${data.repo_full_name}/pull/${data.pr_number}`;
  const isResolved = data.status !== "pending" && data.status !== "in_review";

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-2 space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="text-xs text-muted font-mono truncate">{data.review_id}</div>
            <h1 className="text-xl font-semibold mt-1">
              {data.repo_full_name} #{data.pr_number}
            </h1>
            <p className="text-sm text-muted mt-1">{data.escalation_reason}</p>
            <a
              href={prUrl}
              target="_blank"
              rel="noreferrer"
              className="text-sm text-accent underline mt-2 inline-block"
            >
              Open PR on GitHub →
            </a>
          </div>
          <div className="flex flex-col items-end gap-1 shrink-0">
            <div className="flex items-center gap-1">
              <span className="text-xs text-muted">agent</span>
              <VerdictChip verdict={data.agent_verdict} />
            </div>
            {data.human_verdict && (
              <div className="flex items-center gap-1">
                <span className="text-xs text-muted">human</span>
                <VerdictChip verdict={data.human_verdict} />
              </div>
            )}
          </div>
        </div>

        <FindingsByAgent findings={data.findings ?? []} />

        {isResolved && data.human_reason && (
          <div className="border border-border rounded-lg bg-panel p-4">
            <div className="text-xs uppercase tracking-wide text-muted mb-2">
              Human reason {data.reviewer_id ? `· by ${data.reviewer_id}` : ""}
            </div>
            <p className="text-sm whitespace-pre-wrap">{data.human_reason}</p>
          </div>
        )}
      </div>

      <div className="lg:col-span-1">
        {isResolved ? (
          <div className="border border-border rounded-lg bg-panel p-4 text-sm">
            <div className="text-xs uppercase tracking-wide text-muted mb-2">
              Status
            </div>
            <div className="font-mono">{data.status}</div>
            {data.resolved_at && (
              <div className="text-xs text-muted mt-1">
                resolved {new Date(data.resolved_at).toLocaleString()}
              </div>
            )}
          </div>
        ) : (
          <div className="sticky top-6">
            <h2 className="text-sm uppercase tracking-wide text-muted mb-3">
              Make a decision
            </h2>
            <HITLDecisionForm hitlId={data.id} />
          </div>
        )}
      </div>
    </div>
  );
}
