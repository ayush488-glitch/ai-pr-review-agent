// Typed API client. Single boundary between frontend and backend (Law of Demeter).
// All fetches go through here so swapping transport (polling -> SSE) is one file.

import type {
  BudgetStatus,
  DailyPoint,
  EconomicsSummary,
  HITLDecisionRequest,
  HITLDecisionResponse,
  HITLDetail,
  HITLItem,
  Paginated,
  ReviewDetail,
  ReviewSummary,
  WorkflowCost,
} from "./types";

// Empty BASE = use relative paths so Next.js rewrites (next.config.mjs) proxy
// to the real backend. Sidesteps browser CORS in dev and prod alike.
// To bypass the proxy, set NEXT_PUBLIC_API_DIRECT=1.
const BASE =
  process.env.NEXT_PUBLIC_API_DIRECT === "1"
    ? process.env.NEXT_PUBLIC_API_BASE_URL ?? ""
    : "";

const TOKEN = process.env.NEXT_PUBLIC_API_TOKEN ?? "";
const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "";

function headers(): HeadersInit {
  const h: Record<string, string> = { "content-type": "application/json" };
  if (TOKEN) h["authorization"] = `Bearer ${TOKEN}`;
  if (API_KEY) h["x-api-key"] = API_KEY;
  return h;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { ...headers(), ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${body || path}`);
  }
  return (await res.json()) as T;
}

// SWR fetcher (single-arg)
export const fetcher = <T>(path: string) => req<T>(path);

// Encode a "owner/repo:pr:sha" id for a URL path segment.
// Backend matches on the raw colon-separated id; we URL-encode the slash and colons.
export const encodeReviewId = (id: string) => encodeURIComponent(id);

export const api = {
  health: () => req<{ status: string; version?: string }>("/health/live"),

  listReviews: (limit = 50, offset = 0) =>
    req<Paginated<ReviewSummary>>(
      `/api/v1/reviews?limit=${limit}&offset=${offset}`
    ),
  getReview: (id: string) =>
    req<ReviewDetail>(`/api/v1/reviews/${encodeReviewId(id)}`),

  hitlQueue: (limit = 50, offset = 0) =>
    req<Paginated<HITLItem>>(
      `/api/v1/hitl/queue?limit=${limit}&offset=${offset}`
    ),
  hitlGet: (id: string) =>
    req<HITLDetail>(`/api/v1/hitl/${encodeURIComponent(id)}`),
  hitlDecide: (id: string, body: HITLDecisionRequest) =>
    req<HITLDecisionResponse>(
      `/api/v1/hitl/${encodeURIComponent(id)}/decision`,
      { method: "POST", body: JSON.stringify(body) }
    ),
  hitlRebuild: () =>
    req<{ rebuilt: number }>("/api/v1/hitl/queue/rebuild", { method: "POST" }),

  // ── Phase 16: Economics ────────────────────────────────────────
  econSummary: () => req<EconomicsSummary>("/api/v1/economics/summary"),
  econBudget: () => req<BudgetStatus>("/api/v1/economics/budget"),
  econTimeseries: (days = 30) =>
    req<DailyPoint[]>(`/api/v1/economics/timeseries?days=${days}`),
  econWorkflow: (workflowId: string) =>
    req<WorkflowCost>(
      `/api/v1/economics/workflow/${encodeURIComponent(workflowId)}`
    ),
};
