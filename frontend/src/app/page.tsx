"use client";

import useSWR from "swr";
import Link from "next/link";
import { ReviewStatusBadge } from "@/components/ReviewStatusBadge";
import { VerdictChip } from "@/components/VerdictChip";
import { Empty } from "@/components/Empty";
import type { HITLItem, Paginated, ReviewSummary } from "@/lib/types";

export default function DashboardPage() {
  const { data: reviewsResp, error: reviewsErr } =
    useSWR<Paginated<ReviewSummary>>("/api/v1/reviews?limit=50");
  const { data: hitlResp, error: hitlErr } =
    useSWR<Paginated<HITLItem>>("/api/v1/hitl/queue?limit=50");

  const reviews = reviewsResp?.items ?? [];
  const hitl = hitlResp?.items ?? [];
  const recent = reviews.slice(0, 8);
  const pendingHitl = hitl.filter((h) => h.status === "pending" || h.status === "in_review");
  const escalated = reviews.filter((r) => r.status === "escalated" || r.needs_human_review).length;

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <p className="text-muted text-sm mt-1">
          Live state of the review pipeline. Polls every 5s.
        </p>
      </div>

      <section className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Stat label="Reviews" value={reviewsResp?.total} err={!!reviewsErr} />
        <Stat
          label="HITL pending"
          value={pendingHitl.length}
          err={!!hitlErr}
          tone={pendingHitl.length > 0 ? "warn" : "ok"}
        />
        <Stat label="Needs human review" value={escalated} err={!!reviewsErr} />
      </section>

      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-medium">Recent reviews</h2>
          <Link href="/reviews" className="text-sm text-accent">
            View all →
          </Link>
        </div>
        {reviewsErr ? (
          <Empty>Could not load reviews: {String(reviewsErr.message)}</Empty>
        ) : recent.length === 0 ? (
          <Empty>No reviews yet. Open a PR on a watched repo.</Empty>
        ) : (
          <div className="border border-border rounded-lg bg-panel divide-y divide-border">
            {recent.map((r) => (
              <Link
                key={r.id}
                href={`/reviews/${encodeURIComponent(r.id)}`}
                className="flex items-center justify-between px-4 py-3 hover:bg-bg gap-3"
              >
                <div className="min-w-0">
                  <div className="font-mono text-sm truncate">
                    {r.repo_full_name} #{r.pr_number}
                  </div>
                  <div className="text-xs text-muted truncate mt-0.5">
                    {r.pr_title}
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <VerdictChip verdict={r.verdict} />
                  <ReviewStatusBadge status={r.status} />
                </div>
              </Link>
            ))}
          </div>
        )}
      </section>

      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-medium">HITL queue</h2>
          <Link href="/hitl" className="text-sm text-accent">
            Open queue →
          </Link>
        </div>
        {pendingHitl.length === 0 ? (
          <Empty>Queue is clear.</Empty>
        ) : (
          <div className="border border-border rounded-lg bg-panel divide-y divide-border">
            {pendingHitl.slice(0, 5).map((h) => (
              <Link
                key={h.id}
                href={`/hitl/${encodeURIComponent(h.id)}`}
                className="flex items-center justify-between px-4 py-3 hover:bg-bg gap-3"
              >
                <div className="min-w-0">
                  <div className="font-mono text-sm truncate">
                    {h.repo_full_name} #{h.pr_number}
                  </div>
                  <div className="text-xs text-muted truncate">
                    {h.escalation_reason}
                  </div>
                </div>
                <VerdictChip verdict={h.agent_verdict} />
              </Link>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function Stat({
  label,
  value,
  err,
  tone,
}: {
  label: string;
  value?: number;
  err?: boolean;
  tone?: "ok" | "warn" | "err";
}) {
  const color =
    tone === "warn"
      ? "text-warn"
      : tone === "err"
      ? "text-err"
      : tone === "ok"
      ? "text-ok"
      : "text-white";
  return (
    <div className="border border-border rounded-lg bg-panel px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className={`text-2xl font-mono mt-1 ${color}`}>
        {err ? "—" : value ?? "…"}
      </div>
    </div>
  );
}
