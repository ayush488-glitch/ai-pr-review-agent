"use client";

import useSWR from "swr";
import { Empty } from "@/components/Empty";
import type {
  BudgetStatus,
  DailyPoint,
  EconomicsSummary,
} from "@/lib/types";

export default function EconomicsPage() {
  const { data: summary, error: sumErr } = useSWR<EconomicsSummary>(
    "/api/v1/economics/summary"
  );
  const { data: budget, error: budErr } = useSWR<BudgetStatus>(
    "/api/v1/economics/budget"
  );
  const { data: timeseries, error: tsErr } = useSWR<DailyPoint[]>(
    "/api/v1/economics/timeseries?days=30"
  );

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">Economics</h1>
        <p className="text-muted text-sm mt-1">
          LLM spend, budget headroom, and per-model breakdown. Polls every 5s.
        </p>
      </div>

      {/* Budget gauge */}
      <section className="border border-border rounded-lg bg-panel px-5 py-4">
        <div className="flex items-baseline justify-between">
          <div className="text-xs uppercase tracking-wide text-muted">
            Daily budget
          </div>
          <div className="text-xs text-muted font-mono">
            {budget
              ? `${pct(budget.daily_utilization)} used`
              : budErr
              ? "—"
              : "…"}
          </div>
        </div>
        {budErr ? (
          <Empty>Could not load budget: {String(budErr.message)}</Empty>
        ) : !budget ? (
          <div className="h-3 mt-3 rounded bg-bg border border-border" />
        ) : (
          <>
            <div className="h-3 mt-3 rounded bg-bg border border-border overflow-hidden">
              <div
                className={`h-full transition-all ${
                  budget.exceeded
                    ? "bg-err"
                    : budget.daily_utilization > 0.8
                    ? "bg-warn"
                    : "bg-accent"
                }`}
                style={{
                  width: `${Math.min(100, budget.daily_utilization * 100)}%`,
                }}
              />
            </div>
            <div className="flex justify-between mt-2 text-sm font-mono">
              <span className={budget.exceeded ? "text-err" : "text-white"}>
                {usd(budget.daily_spent_usd)}
              </span>
              <span className="text-muted">
                cap {usd(budget.daily_cap_usd)} · headroom{" "}
                {usd(budget.daily_headroom_usd)}
              </span>
            </div>
            <div className="text-[11px] text-muted mt-2">
              per-review cap (advisory): {usd(budget.per_review_cap_usd)}
              {budget.exceeded && (
                <span className="ml-2 text-err">
                  · BUDGET EXCEEDED — agents will short-circuit & escalate
                </span>
              )}
            </div>
          </>
        )}
      </section>

      {/* Spend cards */}
      <section className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Stat
          label="Today"
          value={summary ? usd(summary.today_usd) : undefined}
          err={!!sumErr}
        />
        <Stat
          label="Last 7 days"
          value={summary ? usd(summary.last_7d_usd) : undefined}
          err={!!sumErr}
        />
        <Stat
          label="Last 30 days"
          value={summary ? usd(summary.last_30d_usd) : undefined}
          err={!!sumErr}
          sub={
            summary
              ? `${summary.call_count_30d} calls · ${fmtTokens(
                  summary.total_input_tokens_30d + summary.total_output_tokens_30d
                )} tokens`
              : undefined
          }
        />
      </section>

      {/* Sparkline */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-medium">Daily spend (30d)</h2>
        </div>
        {tsErr ? (
          <Empty>Could not load timeseries: {String(tsErr.message)}</Empty>
        ) : !timeseries || timeseries.length === 0 ? (
          <Empty>No spend recorded yet.</Empty>
        ) : (
          <Sparkline points={timeseries} />
        )}
      </section>

      {/* Breakdown tables */}
      <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Breakdown
          title="By model (30d)"
          rows={summary?.by_model_30d}
          total={summary?.last_30d_usd}
          err={!!sumErr}
        />
        <Breakdown
          title="By agent (30d)"
          rows={summary?.by_agent_30d}
          total={summary?.last_30d_usd}
          err={!!sumErr}
        />
      </section>
    </div>
  );
}

function Stat({
  label,
  value,
  sub,
  err,
}: {
  label: string;
  value?: string;
  sub?: string;
  err?: boolean;
}) {
  return (
    <div className="border border-border rounded-lg bg-panel px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-muted">{label}</div>
      <div className="text-2xl font-mono mt-1 text-white">
        {err ? "—" : value ?? "…"}
      </div>
      {sub && <div className="text-[11px] text-muted mt-1">{sub}</div>}
    </div>
  );
}

function Breakdown({
  title,
  rows,
  total,
  err,
}: {
  title: string;
  rows?: Record<string, number>;
  total?: number;
  err?: boolean;
}) {
  const entries = rows ? Object.entries(rows).sort((a, b) => b[1] - a[1]) : [];
  return (
    <div className="border border-border rounded-lg bg-panel">
      <div className="px-4 py-3 border-b border-border text-sm font-medium">
        {title}
      </div>
      {err ? (
        <div className="px-4 py-3 text-sm text-muted">—</div>
      ) : !rows ? (
        <div className="px-4 py-3 text-sm text-muted">…</div>
      ) : entries.length === 0 ? (
        <div className="px-4 py-3 text-sm text-muted">No data yet.</div>
      ) : (
        <div className="divide-y divide-border">
          {entries.map(([k, v]) => {
            const share = total && total > 0 ? v / total : 0;
            return (
              <div
                key={k}
                className="flex items-center justify-between px-4 py-2 gap-3"
              >
                <div className="font-mono text-sm truncate">{k}</div>
                <div className="flex items-center gap-3 shrink-0">
                  <div className="w-24 h-1.5 rounded bg-bg overflow-hidden">
                    <div
                      className="h-full bg-accent"
                      style={{ width: `${Math.min(100, share * 100)}%` }}
                    />
                  </div>
                  <div className="font-mono text-sm w-20 text-right">
                    {usd(v)}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function Sparkline({ points }: { points: DailyPoint[] }) {
  // ascending date order; pad with zeros if fewer than expected days.
  const data = [...points].sort((a, b) => a.date.localeCompare(b.date));
  const W = 800;
  const H = 140;
  const PAD = 8;
  const max = Math.max(0.0001, ...data.map((p) => p.cost_usd));
  const stepX = data.length > 1 ? (W - 2 * PAD) / (data.length - 1) : 0;
  const y = (v: number) => H - PAD - (v / max) * (H - 2 * PAD);
  const path = data
    .map((p, i) => `${i === 0 ? "M" : "L"} ${PAD + i * stepX} ${y(p.cost_usd)}`)
    .join(" ");
  const area = `${path} L ${PAD + (data.length - 1) * stepX} ${H - PAD} L ${PAD} ${
    H - PAD
  } Z`;
  const total = data.reduce((s, p) => s + p.cost_usd, 0);
  const last = data[data.length - 1];
  return (
    <div className="border border-border rounded-lg bg-panel p-4">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto">
        <path d={area} fill="rgb(96 165 250 / 0.12)" />
        <path d={path} fill="none" stroke="rgb(96 165 250)" strokeWidth={1.5} />
        {data.map((p, i) => (
          <circle
            key={p.date}
            cx={PAD + i * stepX}
            cy={y(p.cost_usd)}
            r={1.5}
            fill="rgb(96 165 250)"
          />
        ))}
      </svg>
      <div className="flex justify-between text-[11px] text-muted font-mono mt-2">
        <span>{data[0]?.date}</span>
        <span>
          peak {usd(max)} · sum {usd(total)} · last{" "}
          {last ? `${last.date} ${usd(last.cost_usd)}` : "—"}
        </span>
        <span>{data[data.length - 1]?.date}</span>
      </div>
    </div>
  );
}

function usd(n: number): string {
  if (n === 0) return "$0.00";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  if (n < 1) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
}

function pct(n: number): string {
  return `${(n * 100).toFixed(1)}%`;
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}
