"use client";

import useSWR from "swr";
import { useParams } from "next/navigation";
import { FindingsByAgent } from "@/components/AgentFindingCard";
import { ReviewStatusBadge } from "@/components/ReviewStatusBadge";
import { VerdictChip } from "@/components/VerdictChip";
import { Empty } from "@/components/Empty";
import { api } from "@/lib/api";
import type { ReviewDetail } from "@/lib/types";

export default function ReviewDetailPage() {
  const params = useParams<{ id: string }>();
  const id = decodeURIComponent(params.id);
  // Use api.getReview (custom fetcher) instead of the URL-string fetcher so
  // the BE-route fallback in lib/api.ts kicks in for ids containing "/".
  const { data, error, isLoading } = useSWR<ReviewDetail>(
    ["review-detail", id],
    () => api.getReview(id),
    { refreshInterval: 5000 }
  );

  if (error) return <Empty>Failed to load: {error.message}</Empty>;
  if (isLoading || !data) return <Empty>Loading…</Empty>;

  const prUrl = `https://github.com/${data.repo_full_name}/pull/${data.pr_number}`;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="text-xs text-muted font-mono truncate">{data.id}</div>
          <h1 className="text-2xl font-semibold mt-1">
            {data.repo_full_name} #{data.pr_number}
          </h1>
          <div className="text-sm text-muted mt-1">{data.pr_title}</div>
          <div className="text-xs text-muted font-mono mt-2">
            {data.head_commit_sha}
          </div>
          <a
            href={prUrl}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-accent underline mt-2 inline-block"
          >
            Open PR on GitHub →
          </a>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <ReviewStatusBadge status={data.status} />
          <VerdictChip verdict={data.verdict} />
        </div>
      </div>

      {data.overall_confidence != null && (
        <div className="text-xs text-muted">
          Overall confidence: {(data.overall_confidence * 100).toFixed(0)}%
        </div>
      )}

      <FindingsByAgent findings={data.findings ?? []} />

      {data.human_review_reason && (
        <div className="border border-border rounded-lg bg-panel p-4">
          <div className="text-xs uppercase tracking-wide text-muted mb-2">
            Human review reason
          </div>
          <p className="text-sm whitespace-pre-wrap">{data.human_review_reason}</p>
        </div>
      )}
    </div>
  );
}
