/**
 * ConsoleView (CONS-01..04) — the Observability Console page. The integration point where the tested
 * wave-1/2 pieces (filter/scale math, the SSE hook + data layer, the SVG charts, the feed + inspector)
 * become the live console. This is the ONE deliberately-live section of an otherwise static, no-poll SPA.
 *
 * The load-bearing shape (RESEARCH Pattern 7 — one filter, four consumers): a SINGLE shared
 * `{ status, route, window }` filter + a single `selected` event, both owned here, drive ALL of:
 *   (A) the filtered live feed + the client-derived by-route/by-status counts + the header KPIs,
 *   (B) the AGGREGATE latency chart (server `/history`, window-sliced; route/status do NOT apply — the
 *       server series is aggregate-only, D-07 / Pitfall 4),
 *   (C) the DRILL-DOWN latency chart (client-recomputed from the filtered buffer via `drilldownBuckets`
 *       when a route/status filter is active), behind the aggregate↔drill-down mode toggle.
 *
 * Data discipline (Pitfall 3): `/stats` is fetched ONCE to seed the live buffer (`useEventStream` seed),
 * `/history` backs the aggregate chart, and the feed is live via SSE — there is NO repeated full-`recent`
 * poll (the query hooks set no poll interval). The buffer seeding requires `stats.recent[]` to be present at the
 * moment `useEventStream` mounts, so the stream + composition live in an inner `<ConsoleBody>` that is
 * rendered only after `/stats` resolves (its `useState(seed)` initializer then captures the real seed).
 *
 * Security: no `dangerouslySetInnerHTML`, no `armGet` — the console fetches are bearer-exempt
 * (`consoleGet`/`simGet`) and every scanner-controlled string reaches the DOM as auto-escaped JSX text
 * via the child components (T-16-01 / T-16-03).
 */
import { useState } from 'react';

import KpiStat from '../explorer/KpiStat';
import {
  useConsoleStats,
  useConsoleHistory,
  type StatsSnapshot,
  type HistorySnapshot,
  type RequestEvent,
} from '../api/console';
import {
  matches,
  sliceByWindow,
  drilldownBuckets,
  statusClass,
  WINDOW_MS,
  type ConsoleFilter,
  type StatusClass,
  type WindowPreset,
} from './filter';
import { useEventStream } from './useEventStream';
import ConsoleFilters, { DEFAULT_FILTER } from './ConsoleFilters';
import LiveStatusPill from './LiveStatusPill';
import LatencyChart from './LatencyChart';
import CountBars from './CountBars';
import FeedTable from './FeedTable';
import RequestInspector from './RequestInspector';
import styles from './ConsoleView.module.css';

/** Buffer cap — matches the grown server ring depth so client-side window filters have equal depth. */
const CAP = 2000;

/** A ConsoleEvent is structurally a RequestEvent — the filter helpers are typed over the former. */
type Ev = RequestEvent;

/** Group items into a `{ key → count }` record (client-derived counts over the filtered buffer). */
function countBy(items: readonly Ev[], key: (e: Ev) => string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const it of items) {
    const k = key(it);
    out[k] = (out[k] ?? 0) + 1;
  }
  return out;
}

/** Human window label for the count-block sub-label (`· last 5m`). */
function windowLabel(w: WindowPreset): string {
  return w === 'all' ? 'all' : w;
}

/** Slice a server history snapshot to the active window (route/status never apply — D-07). */
function sliceHistoryToWindow(h: HistorySnapshot, window: WindowPreset): HistorySnapshot {
  const wMs = WINDOW_MS[window];
  if (wMs === null) return h;
  const cutoff = h.server_now_ms - wMs;
  return { ...h, buckets: h.buckets.filter((b) => b.ts_ms >= cutoff), window_ms: wMs };
}

export default function ConsoleView() {
  const stats = useConsoleStats();
  const history = useConsoleHistory();

  const header = (
    <header className={styles.head}>
      <div className={styles.eyebrowRow}>
        <div className={styles.eyebrow}>◆ Observability console</div>
      </div>
      <h1 className={styles.title}>Observability Console</h1>
    </header>
  );

  // Gate the live composition on the one-shot `/stats` seed so `useEventStream` captures the real
  // `recent[]` buffer at mount (Pitfall 3). Keep the header visible in every state.
  if (stats.isLoading) {
    return (
      <section className={styles.view} aria-label="Observability console">
        {header}
        <div className={styles.skeleton} aria-label="Loading console" />
      </section>
    );
  }

  if (stats.isError || !stats.data) {
    return (
      <section className={styles.view} aria-label="Observability console">
        {header}
        <div className={styles.errorRow}>
          <span className={styles.errorText}>Could not load console metrics.</span>
          <button type="button" className={styles.retry} onClick={() => void stats.refetch()}>
            Retry
          </button>
        </div>
      </section>
    );
  }

  return (
    <ConsoleBody
      stats={stats.data}
      history={history.data}
      historyLoading={history.isLoading}
      historyError={history.isError}
      onHistoryRetry={() => void history.refetch()}
      headerNode={header}
    />
  );
}

interface ConsoleBodyProps {
  stats: StatsSnapshot;
  history: HistorySnapshot | undefined;
  historyLoading: boolean;
  historyError: boolean;
  onHistoryRetry: () => void;
  headerNode: React.ReactNode;
}

function ConsoleBody({
  stats,
  history,
  historyLoading,
  historyError,
  onHistoryRetry,
  headerNode,
}: ConsoleBodyProps) {
  // ── The single shared filter + selection (RESEARCH Pattern 7): one source for all four consumers ──
  const [filter, setFilter] = useState<ConsoleFilter>(DEFAULT_FILTER);
  const [selected, setSelected] = useState<RequestEvent | null>(null);
  const [mode, setMode] = useState<'aggregate' | 'drilldown'>('aggregate');

  // ── The one live data path: seed ONCE from stats.recent, then SSE prepends (no poll) ──
  const stream = useEventStream(CAP, stats.recent);
  const [paused, setPaused] = useState(false);
  const togglePause = () => {
    const next = !paused;
    setPaused(next);
    stream.pause(next);
  };

  const nowMs = Date.now();

  // ── Consumer A: the filtered live buffer → feed rows + client counts + KPIs ──
  const windowed = sliceByWindow(stream.events, nowMs, filter.window);
  const filtered = windowed.filter((ev) => matches(ev, filter));

  const filterActive = filter.status.size > 0 || filter.route !== null;
  // Session "overview" counters only when nothing narrows the view (no filter, unbounded window).
  const overview = !filterActive && filter.window === 'all';

  // KPIs over the filtered buffer (reuse the tested nearest-rank percentile via a single mega-bucket).
  const kpiAgg = drilldownBuckets(filtered, Number.MAX_SAFE_INTEGER);
  const kpi = kpiAgg.length > 0 ? kpiAgg[0] : null;
  const total = filtered.length;
  const wMs = WINDOW_MS[filter.window];
  const windowSeconds =
    wMs !== null
      ? wMs / 1000
      : Math.max(1, (nowMs - (filtered.length > 0 ? filtered[filtered.length - 1].ts_ms : nowMs)) / 1000);
  const reqPerSec = Math.round((total / windowSeconds) * 10) / 10;

  // ── Consumer counts: server session counters (overview) vs client-derived over the filtered buffer ──
  const routeCounts = overview ? stats.by_route : countBy(filtered, (e) => e.route);
  const statusCounts = overview
    ? stats.by_status
    : countBy(filtered, (e) => statusClass(e.status));
  const countSuffix = overview ? '· session' : `· last ${windowLabel(filter.window)}`;

  // ── Consumers B/C: aggregate (server, window-sliced) vs drill-down (client) latency chart ──
  const aggregateHistory = history ? sliceHistoryToWindow(history, filter.window) : undefined;
  const drilldownSeries = drilldownBuckets(filtered);
  const drilldownLabel = filter.route ?? [...filter.status].join(' ');
  const effectiveMode: 'aggregate' | 'drilldown' =
    mode === 'drilldown' && filterActive ? 'drilldown' : 'aggregate';
  const chartWindowMs = wMs ?? history?.window_ms ?? 300_000;

  // ── Feed + count selection wiring ──
  const selectedStatus: string | null =
    filter.status.size === 1 ? [...filter.status][0] : null;

  const onRouteSelect = (key: string | null) => setFilter({ ...filter, route: key });
  const onStatusSelect = (key: string | null) => {
    if (key === null) {
      setFilter({ ...filter, status: new Set() });
      return;
    }
    const cls: StatusClass = /^\d+$/.test(key) ? statusClass(Number(key)) : (key as StatusClass);
    setFilter({ ...filter, status: new Set([cls]) });
  };

  const emptyKind: 'no-traffic' | 'filtered' =
    filterActive && stream.events.length > 0 ? 'filtered' : 'no-traffic';

  return (
    <section className={styles.view} aria-label="Observability console">
      <div className={styles.headBand}>
        {headerNode}
        <div className={styles.controls}>
          <LiveStatusPill status={stream.status} paused={paused} onToggle={togglePause} />
        </div>
      </div>

      <div className={styles.kpis}>
        <KpiStat value={kpi ? kpi.p50_ms : 0} label="p50 ms" />
        <KpiStat value={kpi ? kpi.p95_ms : 0} label="p95 ms" />
        <KpiStat value={kpi ? kpi.max_ms : 0} label="max ms" />
        <KpiStat value={total} label={overview ? 'requests' : `req · ${windowLabel(filter.window)}`} />
        <KpiStat value={reqPerSec} label="req/s" />
      </div>

      <ConsoleFilters filter={filter} routes={Object.keys(stats.by_route)} onChange={setFilter} />

      <div className={styles.chartHead}>
        <div className={styles.modeToggle} role="group" aria-label="Latency chart mode">
          <button
            type="button"
            data-active={effectiveMode === 'aggregate' || undefined}
            className={effectiveMode === 'aggregate' ? styles.modeActive : styles.modeBtn}
            onClick={() => setMode('aggregate')}
          >
            aggregate
          </button>
          <button
            type="button"
            data-active={effectiveMode === 'drilldown' || undefined}
            className={effectiveMode === 'drilldown' ? styles.modeActive : styles.modeBtn}
            disabled={!filterActive}
            onClick={() => setMode('drilldown')}
          >
            drill-down
          </button>
        </div>
      </div>

      <LatencyChart
        history={aggregateHistory}
        isLoading={historyLoading}
        isError={historyError}
        onRetry={onHistoryRetry}
        windowMs={chartWindowMs}
        mode={effectiveMode}
        drilldownSeries={drilldownSeries}
        drilldownLabel={drilldownLabel}
        filterActive={filterActive}
      />

      <div className={styles.countsRow}>
        <CountBars
          variant="route"
          counts={routeCounts}
          selected={filter.route}
          onSelect={onRouteSelect}
          label={`by route ${countSuffix}`}
        />
        <CountBars
          variant="status"
          counts={statusCounts}
          selected={selectedStatus}
          onSelect={onStatusSelect}
          label={`by status ${countSuffix}`}
        />
      </div>

      <div className={styles.feedRow}>
        <div className={styles.feedCol}>
          <div className={styles.blockTitle}>◆ Live feed</div>
          <FeedTable
            events={filtered}
            selected={selected}
            onSelect={setSelected}
            state={stream.status}
            emptyKind={emptyKind}
            nowMs={nowMs}
          />
        </div>
        <div className={styles.inspectorCol}>
          <div className={styles.blockTitle}>◆ Request inspector</div>
          <RequestInspector selected={selected} nowMs={nowMs} />
        </div>
      </div>
    </section>
  );
}
