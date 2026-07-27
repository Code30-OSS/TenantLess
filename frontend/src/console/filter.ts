/**
 * Pure client-side filter + drill-down math for the Observability Console (CONS-02 / CONS-04).
 * No fetch, no React, no DOM — these functions only classify, predicate, slice, and re-bucket the
 * in-memory event buffer (seeded from `/_console/stats.recent[]`, appended by SSE). Mirrors the
 * pure `api/odata.ts` idiom: small exported functions, edge-tolerant, never throwing. The filter
 * UI + charts (16-05/16-06/16-07) build against these tested contracts.
 *
 * Why client-side: the server `/_console/history` series is a single AGGREGATE p50/p95/max stream
 * (D-07 keeps it query-param-free — no per-route/per-status breakdown, RESEARCH Pitfall 4). The
 * only way to get a per-route/per-status latency series is to recompute it here from the filtered
 * event buffer — that is what {@link drilldownBuckets} does, reusing the same nearest-rank
 * percentile the server's `metrics.rs::percentile` uses so the numbers agree.
 *
 * `statusClass` is owned SOLELY by this module — it is the canonical status-classing helper for
 * the Console; consumers import it from here rather than defining their own copy.
 *
 * The scanner-controlled `route`/`path` strings only ever flow through string comparison and Map
 * grouping here — no HTML, no `eval` (T-16-03: XSS-safe rendering is the JSX consumers' job).
 */

/** A served request as recorded by the mock-server metrics ring (`RequestEvent`, metrics.rs). */
export interface ConsoleEvent {
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

/** HTTP status class (hundreds band) — the drill-down + filter status domain (CONS-04). */
export type StatusClass = '2xx' | '3xx' | '4xx' | '5xx';

/** Preset time-window keys (D-08). `all` = unbounded (no cutoff). */
export type WindowPreset = '1m' | '5m' | '15m' | 'all';

/**
 * The single Console filter state, three consumers (RESEARCH Pattern 7):
 * - `status`: a multi-select set of status classes. Empty = all (pass-through).
 * - `route`: a single selected route template, or `null` = all (pass-through).
 * - `window`: a preset time window; slices both the client buffer and the server history series.
 */
export interface ConsoleFilter {
  status: Set<StatusClass>;
  route: string | null;
  window: WindowPreset;
}

/** One recomputed drill-down latency bucket (client-side per-route/per-status series, D-07). */
export interface DrilldownBucket {
  /** Bucket start in epoch ms (`bucketKey * bucketMs`). */
  ts_ms: number;
  count: number;
  p50_ms: number;
  p95_ms: number;
  max_ms: number;
}

/** Preset window durations in ms; `all` maps to `null` (no cutoff). */
export const WINDOW_MS: Record<WindowPreset, number | null> = {
  '1m': 60_000,
  '5m': 300_000,
  '15m': 900_000,
  all: null,
};

/**
 * Class a numeric status into its hundreds band, clamped into the `2xx`–`5xx` domain so an
 * out-of-range status (e.g. a stray 1xx / 6xx) never yields an invalid class. Canonical
 * status-classing helper for the Console — do not re-define elsewhere.
 */
export function statusClass(status: number): StatusClass {
  const band = Math.min(5, Math.max(2, Math.floor(status / 100)));
  return `${band}xx` as StatusClass;
}

/**
 * Pure inclusion predicate for one event against the filter. An event passes when its status
 * class is in the `status` set (or the set is empty) AND its `route` equals the selected `route`
 * (or `route` is null). An all-empty filter passes every event through.
 */
export function matches(ev: ConsoleEvent, filter: ConsoleFilter): boolean {
  if (filter.status.size > 0 && !filter.status.has(statusClass(ev.status))) return false;
  if (filter.route !== null && ev.route !== filter.route) return false;
  return true;
}

/**
 * Compute the epoch-ms cutoff for a window preset relative to `nowMs`: `nowMs - WINDOW_MS[window]`,
 * or `null` for the unbounded `all` preset (no cutoff — every event is in-window).
 */
export function windowCutoffMs(nowMs: number, window: WindowPreset): number | null {
  const ms = WINDOW_MS[window];
  return ms === null ? null : nowMs - ms;
}

/**
 * Slice an event buffer to a preset window: keep only events with `ts_ms >= cutoff` where the
 * cutoff is {@link windowCutoffMs}. Returns every event for the `all` window (null cutoff).
 * Preserves input order; never mutates the input.
 */
export function sliceByWindow(
  events: readonly ConsoleEvent[],
  nowMs: number,
  window: WindowPreset,
): ConsoleEvent[] {
  const cutoff = windowCutoffMs(nowMs, window);
  if (cutoff === null) return events.slice();
  return events.filter((ev) => ev.ts_ms >= cutoff);
}

/**
 * Nearest-rank percentile over an ascending-sorted numeric array (port of the server's
 * `metrics.rs::percentile`: `idx = ceil(p/100 * N) - 1`, clamped). Empty array → 0.
 */
function percentile(sorted: readonly number[], p: number): number {
  if (sorted.length === 0) return 0;
  const rank = Math.ceil((p / 100) * sorted.length);
  const idx = Math.min(Math.max(rank - 1, 0), sorted.length - 1);
  return sorted[idx];
}

/**
 * Recompute a client-side drill-down latency series from an (already filtered) event buffer.
 * Groups events by `Math.floor(ts_ms / bucketMs)` and, per bucket, returns the request count and
 * nearest-rank p50/p95/max of the bucket's latencies. Buckets are returned ascending by time;
 * empty groups are omitted. This is the D-07 per-route/per-status series the server cannot
 * provide — never a server call.
 */
export function drilldownBuckets(
  events: readonly ConsoleEvent[],
  bucketMs = 1000,
): DrilldownBucket[] {
  const groups = new Map<number, number[]>();
  for (const ev of events) {
    const key = Math.floor(ev.ts_ms / bucketMs);
    let arr = groups.get(key);
    if (arr === undefined) {
      arr = [];
      groups.set(key, arr);
    }
    arr.push(ev.latency_ms);
  }
  return [...groups.keys()]
    .sort((a, b) => a - b)
    .map((key) => {
      const lat = groups.get(key)!.slice().sort((a, b) => a - b);
      return {
        ts_ms: key * bucketMs,
        count: lat.length,
        p50_ms: percentile(lat, 50),
        p95_ms: percentile(lat, 95),
        max_ms: lat[lat.length - 1],
      };
    });
}
