"use client";

import { SWRConfig } from "swr";
import { fetcher } from "./api";

export function SWRProvider({ children }: { children: React.ReactNode }) {
  return (
    <SWRConfig
      value={{
        fetcher,
        refreshInterval: 5000, // poll every 5s; ADR-003 (polling chosen over SSE)
        revalidateOnFocus: true,
        shouldRetryOnError: false,
      }}
    >
      {children}
    </SWRConfig>
  );
}
