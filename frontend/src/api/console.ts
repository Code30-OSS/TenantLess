/**
 * The bearer-EXEMPT Console data layer (CONS-01 / CONS-04) — DTOs + one-shot fetch hooks for
 * `/_console/stats` and `/_console/history`, plus the shared status→token color map the SVG + feed
 * views consume. Mirrors the `api/queries.ts` hook idiom and the `api/types.ts` DTO idiom, routed
 * through the SAME same-origin guard the Explorer uses.
 *
 * Routing discipline (RESEARCH Pattern 5 / "Don't Hand-Roll"): the `/_console/**` routes live on the
 * server's UNINSTRUMENTED, bearer-EXEMPT console router (outside the ARM bearer + record_metrics
 * layers). So {@link consoleGet} delegates to {@link simGet} — the same `assertSameOrigin` fail-closed
 * guard, and crucially NO `Authorization` header. We NEVER use the ARM `/subscriptions/**` fetch
 * wrapper here: that attaches a placeholder Bearer meant only for the ARM gate; leaking it to a
 * console route would both be wrong (bearer-exempt) and a needless credential on the wire (T-16-01).
 *
 * One-shot seed (RESEARCH Pitfall 3): the event ring is now ~2000 deep, so `/stats.recent[]` is a
 * large payload — {@link useConsoleStats} fetches it ONCE to seed the client buffer (then live SSE
 * prepends via `console/useEventStream`); it sets no `refetchInterval`, so there is no repeated
 * full-`recent` poll. {@link useConsoleHistory} is the ONE exception: the aggregate latency series is a
 * small, `recent[]`-free bucket array, and the chart's x-axis is pinned to the snapshot's
 * `server_now_ms`, so a one-shot fetch leaves the chart frozen. It therefore polls on a light interval
 * ({@link HISTORY_REFETCH_MS}) so the history chart actually advances with live traffic — this is the
 * console's single deliberately-live query (ConsoleView is "the ONE deliberately-live section").
 *
 * The numeric-status → `2xx`–`5xx` band classifier is owned SOLELY by `console/filter.ts` (Plan
 * 16-02) — this module deliberately does NOT define or re-export it. It exports only the color map
 * {@link STATUS_TOKEN}; consumers import the band classifier from `console/filter.ts` and
 * `STATUS_TOKEN` from here, so there is exactly one owner of each (no divergent Wave-1 copy).
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { simGet } from './client';

// ---------------------------------------------------------------------------
// DTOs — mirror the server structs (metrics.rs, Plan 16-01/16-04) verbatim
// ---------------------------------------------------------------------------

/** One served request as recorded by the mock-server metrics ring (`RequestEvent`, metrics.rs). */
export interface RequestEvent {
  /** Unix epoch milliseconds when the response completed. */
  ts_ms: number;
  method: string;
  /** Concrete request path (with real subscription / RG ids). */
  path: string;
  /** Matched route template, used for the per-route breakdown. */
  route: string;
  status: number;
  latency_ms: number;
}

/**
 * `GET /_console/stats` — the one-shot snapshot: lifetime counters, current p50/p95/max, and the
 * newest-first `recent[]` ring slice used ONCE to seed the live buffer (never re-polled, Pitfall 3).
 */
export interface StatsSnapshot {
  total: number;
  by_status: Record<string, number>;
  by_route: Record<string, number>;
  p50_ms: number;
  p95_ms: number;
  max_ms: number;
  /** Newest-first slice of the event ring — the one-shot seed for `useEventStream`. */
  recent: RequestEvent[];
}

/**
 * One bucket of the server latency-history series. Empty (no-traffic) buckets carry `count: 0` and
 * **null** percentiles — NOT `0` (a zero would drag the latency line to the axis and lie; the chart
 * breaks the polyline on null). Mirrors serde `Option<u64>` on the server (metrics.rs, Pitfall 5).
 */
export interface HistoryBucket {
  ts_ms: number;
  count: number;
  p50_ms: number | null;
  p95_ms: number | null;
  max_ms: number | null;
}

/**
 * `GET /_console/history` — the AGGREGATE p50/p95/max over-time series (D-03). One series only; it
 * carries no per-route/per-status breakdown (D-07 keeps it query-param-free — drill-down is recomputed
 * client-side in `console/filter.ts`). `server_now_ms` lets the client align the x-axis to "now".
 */
export interface HistorySnapshot {
  bucket_ms: number;
  window_ms: number;
  server_now_ms: number;
  /** Oldest → newest, one entry per window bucket (empty buckets have null percentiles). */
  buckets: HistoryBucket[];
}

// ---------------------------------------------------------------------------
// Shared status → color token map (never a raw hex — directly assertable)
// ---------------------------------------------------------------------------

/**
 * HTTP status class → design-token name (UI-SPEC), mirroring the `ViolationChip.SEVERITY_TOKEN`
 * idiom. Token NAMES only (`--green`/`--amber`/…) — consumers render `var(${STATUS_TOKEN[cls]})`, so a
 * theme swap (dark/light) is automatic and there is no hard-coded hex anywhere. The class key comes
 * from the band classifier in `console/filter.ts` (single owner).
 */
export const STATUS_TOKEN: Record<'2xx' | '3xx' | '4xx' | '5xx', string> = {
  '2xx': '--green',
  '3xx': '--text-2',
  '4xx': '--amber',
  '5xx': '--red',
};

// ---------------------------------------------------------------------------
// Fetch wrapper + one-shot query hooks
// ---------------------------------------------------------------------------

/**
 * GET a bearer-EXEMPT `/_console/**` route. Delegates to {@link simGet} to REUSE the exact same
 * `assertSameOrigin` fail-closed guard (WR-01) with NO `Authorization` header — never the ARM Bearer
 * wrapper. A thin, explicitly-named wrapper so the console call sites read as "console" and the
 * bearer-exempt rationale lives in one place (threat T-16-01).
 */
export async function consoleGet<T>(path: string): Promise<T> {
  return simGet<T>(path);
}

/**
 * One-shot tenant metrics snapshot (`/_console/stats`). Seeds the live-feed buffer + header KPIs; no
 * `refetchInterval` (Pitfall 3 — the big `recent[]` is fetched once, then SSE takes over).
 */
export function useConsoleStats(): UseQueryResult<StatsSnapshot, Error> {
  return useQuery({
    queryKey: ['console-stats'],
    queryFn: () => consoleGet<StatsSnapshot>('/_console/stats'),
  });
}

/**
 * Poll cadence for the aggregate latency history (ms). The server buckets at 1s (`BUCKET_MS`), so a
 * few-second refresh keeps the chart visibly advancing under live traffic without hammering the
 * (bearer-exempt, in-memory) endpoint. This is a small buckets-only payload — NOT the heavy
 * `recent[]` seed that Pitfall 3 forbids re-polling.
 */
export const HISTORY_REFETCH_MS = 3_000;

/**
 * The aggregate latency-history series (`/_console/history`) that backs the overall p50/p95/max line
 * chart (D-03, survives reload). Polls every {@link HISTORY_REFETCH_MS} — the chart's x-axis is pinned
 * to the snapshot's `server_now_ms`, so without a refresh it would freeze at mount; this is the one
 * deliberately-live query (the `recent[]`-free payload keeps the poll cheap, distinct from Pitfall 3).
 */
export function useConsoleHistory(): UseQueryResult<HistorySnapshot, Error> {
  return useQuery({
    queryKey: ['console-history'],
    queryFn: () => consoleGet<HistorySnapshot>('/_console/history'),
    refetchInterval: HISTORY_REFETCH_MS,
  });
}
