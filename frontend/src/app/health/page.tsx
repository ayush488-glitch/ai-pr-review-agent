"use client";

import useSWR from "swr";

export default function HealthPage() {
  const { data, error } = useSWR<{ status: string }>("/health/live");

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Health</h1>
      <div className="border border-border rounded-lg bg-panel p-4 font-mono text-sm">
        {error ? (
          <span className="text-err">DOWN — {error.message}</span>
        ) : !data ? (
          <span className="text-muted">checking…</span>
        ) : (
          <span className="text-ok">UP — {JSON.stringify(data)}</span>
        )}
      </div>
    </div>
  );
}
