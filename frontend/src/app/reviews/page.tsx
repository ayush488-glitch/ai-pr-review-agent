"use client";

import useSWR from "swr";
import Link from "next/link";
import { ReviewStatusBadge } from "@/components/ReviewStatusBadge";
import { VerdictChip } from "@/components/VerdictChip";
import { Empty } from "@/components/Empty";
import type { Paginated, ReviewSummary } from "@/lib/types";

export default function ReviewsPage() {
  const { data, error, isLoading } =
    useSWR<Paginated<ReviewSummary>>("/api/v1/reviews?limit=100");

  const items = data?.items ?? [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Reviews</h1>
        <p className="text-muted text-sm mt-1">
          All PR reviews handled by the agent. {data ? `(${data.total} total)` : ""}
        </p>
      </div>

      {error ? (
        <Empty>Failed to load: {error.message}</Empty>
      ) : isLoading ? (
        <Empty>Loading…</Empty>
      ) : items.length === 0 ? (
        <Empty>No reviews yet.</Empty>
      ) : (
        <div className="border border-border rounded-lg bg-panel overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-bg text-muted text-xs uppercase tracking-wide">
              <tr>
                <th className="text-left px-4 py-2">Repo / PR</th>
                <th className="text-left px-4 py-2">Commit</th>
                <th className="text-left px-4 py-2">Findings</th>
                <th className="text-left px-4 py-2">Verdict</th>
                <th className="text-left px-4 py-2">Status</th>
                <th className="text-left px-4 py-2">Created</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {items.map((r) => (
                <tr key={r.id} className="hover:bg-bg">
                  <td className="px-4 py-3 font-mono">
                    <Link
                      href={`/reviews/${encodeURIComponent(r.id)}`}
                      className="text-accent"
                    >
                      {r.repo_full_name} #{r.pr_number}
                    </Link>
                    <div className="text-xs text-muted truncate max-w-xs">
                      {r.pr_title}
                    </div>
                  </td>
                  <td className="px-4 py-3 font-mono text-muted">
                    {r.head_commit_sha?.slice(0, 7)}
                  </td>
                  <td className="px-4 py-3">{r.finding_count ?? "—"}</td>
                  <td className="px-4 py-3">
                    <VerdictChip verdict={r.verdict} />
                  </td>
                  <td className="px-4 py-3">
                    <ReviewStatusBadge status={r.status} />
                  </td>
                  <td className="px-4 py-3 text-muted text-xs">
                    {new Date(r.created_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
