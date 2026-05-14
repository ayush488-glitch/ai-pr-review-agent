"use client";

import useSWR from "swr";
import Link from "next/link";
import { Empty } from "@/components/Empty";
import { VerdictChip } from "@/components/VerdictChip";
import type { HITLItem, Paginated } from "@/lib/types";

export default function HITLQueuePage() {
  const { data, error, isLoading } =
    useSWR<Paginated<HITLItem>>("/api/v1/hitl/queue?limit=100");

  const items = data?.items ?? [];
  const pending = items.filter((h) => h.status === "pending" || h.status === "in_review");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">HITL Queue</h1>
        <p className="text-muted text-sm mt-1">
          Items escalated by 2+ agents flagging CRITICAL.
        </p>
      </div>

      {error ? (
        <Empty>Failed to load: {error.message}</Empty>
      ) : isLoading ? (
        <Empty>Loading…</Empty>
      ) : pending.length === 0 ? (
        <Empty>Queue is clear.</Empty>
      ) : (
        <div className="border border-border rounded-lg bg-panel divide-y divide-border">
          {pending.map((h) => (
            <Link
              key={h.id}
              href={`/hitl/${encodeURIComponent(h.id)}`}
              className="flex items-start justify-between px-4 py-3 hover:bg-bg gap-4"
            >
              <div className="min-w-0 flex-1">
                <div className="font-mono text-sm truncate">
                  {h.repo_full_name} #{h.pr_number}
                </div>
                <div className="text-sm text-muted mt-0.5 truncate">
                  {h.escalation_reason}
                </div>
                <div className="text-xs text-muted font-mono mt-1">
                  conf {(h.overall_confidence * 100).toFixed(0)}% ·{" "}
                  {new Date(h.created_at).toLocaleString()}
                </div>
              </div>
              <VerdictChip verdict={h.agent_verdict} />
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
