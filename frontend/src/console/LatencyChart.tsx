/**
 * LatencyChart — the CONS-01 zero-dependency, hand-rolled SVG latency line chart (D-05). Renders the
 * p50 / p95 / max series over the active history window as three `<polyline>` runs on a `viewBox
 * 0 0 720 180` plot, with token-only colors (p50 `--green` / p95 `--amber` / max `--red`), a low-opacity
 * p95 area, gridlines + Space-Mono tick labels, and an on-hover guide line + tooltip.
 *
 * All the domain→pixel math lives in the pure, unit-tested `scale.ts` (16-02): `linScale` maps index→x
 * and ms→y, `niceMax` picks the y-axis ceiling, and `polylinePoints` splits each series into one run
 * per contiguous non-null segment — so an idle (null) bucket BREAKS the line instead of dragging it to
 * zero (RESEARCH Pitfall 5 — absence is not zero latency). This component reinvents no chart math.
 *
 * The four states follow the shipped panel-state ladder (loading `pulse` skeleton / muted empty note /
 * inline error + Retry reusing the `ViolationChip .retry` idiom / populated). Every scanner-controlled
 * value reaches the DOM as auto-escaped JSX text (T-16-03: no `dangerouslySetInnerHTML`; colors are
 * `var(--token)` strings, never interpolated user input).
 */
import { useCallback, useRef, useState } from 'react';

import type { HistoryBucket, HistorySnapshot } from '../api/console';
import {
  linScale,
  niceMax,
  polylinePoints,
  VIEWBOX_W,
  VIEWBOX_H,
  INSET_LEFT,
  INSET_BOTTOM,
  INSET_TOP,
  INSET_RIGHT,
} from './scale';
import styles from './LatencyChart.module.css';

/**
 * Inner-plot pixel bounds (all math via `scale.ts`). Left/top/bottom are fixed; the RIGHT edge is
 * derived from the measured container width per-render (the chart is width-responsive), so it lives
 * inside the component. `VIEWBOX_W` is only the initial/SSR default width until the container is
 * measured.
 */
const PLOT_LEFT = INSET_LEFT;
const PLOT_TOP = INSET_TOP;
const PLOT_BOTTOM = VIEWBOX_H - INSET_BOTTOM;

/** The three latency series and their locked semantic color tokens (UI-SPEC CONS-01). */
const SERIES: ReadonlyArray<{
  key: 'p50' | 'p95' | 'max';
  token: string;
  pick: (b: HistoryBucket) => number | null;
}> = [
  { key: 'p50', token: '--green', pick: (b) => b.p50_ms },
  { key: 'p95', token: '--amber', pick: (b) => b.p95_ms },
  { key: 'max', token: '--red', pick: (b) => b.max_ms },
];

interface LatencyChartProps {
  /** The aggregate server history series (`GET /_console/history`); `undefined` until loaded. */
  history: HistorySnapshot | undefined;
  isLoading: boolean;
  isError: boolean;
  /** Re-fetch the history series (wired to the error-state Retry button). */
  onRetry: () => void;
  /** Active window span in ms — labels the x-axis relative-time ticks. */
  windowMs: number;
  /** `aggregate` reads the server series; `drilldown` renders a client-computed per-filter series. */
  mode: 'aggregate' | 'drilldown';
  /** Client-computed per-route/per-status buckets (drill-down mode only). */
  drilldownSeries?: HistoryBucket[];
  /** The drill-down subject label (e.g. a route template or status class) for the mode note. */
  drilldownLabel?: string;
  /** Whether a route/status filter is active — surfaces the aggregate-mode advisory note. */
  filterActive?: boolean;
}

/** Format a positive elapsed-ms delta as a compact relative time (`-2m14s`, `-9s`, `now`). */
function relTime(deltaMs: number): string {
  const s = Math.max(0, Math.round(deltaMs / 1000));
  if (s === 0) return 'now';
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return m > 0 ? `-${m}m${rem}s` : `-${rem}s`;
}

/** Thousands-grouped integer ms, mirroring the KpiStat numeric format (`KpiStat.tsx:15`). */
function fmtMs(v: number | null): string {
  return v === null ? '—' : `${Math.round(v).toLocaleString('en-US')} ms`;
}

/** Build a filled-area `<path>` under one p95 polyline run, closed down to the plot baseline. */
function areaPath(run: string): string {
  const pts = run.split(' ');
  if (pts.length === 0 || pts[0] === '') return '';
  const firstX = pts[0].split(',')[0];
  const lastX = pts[pts.length - 1].split(',')[0];
  return `M ${firstX},${PLOT_BOTTOM} L ${pts.join(' L ')} L ${lastX},${PLOT_BOTTOM} Z`;
}

export default function LatencyChart({
  history,
  isLoading,
  isError,
  onRetry,
  windowMs,
  mode,
  drilldownSeries,
  drilldownLabel,
  filterActive = false,
}: LatencyChartProps) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  // Width-responsive rendering: measure the plot container and render the SVG at its REAL pixel
  // width (viewBox width == measured width), so a wide screen no longer stretches a fixed 720-wide
  // viewBox (which distorts strokes AND axis text under preserveAspectRatio="none"). Height stays
  // VIEWBOX_H, so the box is 1:1 in both axes. A callback ref (re)attaches the observer whenever the
  // plot node mounts (e.g. loading→populated); the `w > 0` guard keeps jsdom (no layout) at the
  // VIEWBOX_W default so the geometry tests are stable.
  const [width, setWidth] = useState<number>(VIEWBOX_W);
  const roRef = useRef<ResizeObserver | null>(null);
  const measureRef = useCallback((el: HTMLDivElement | null) => {
    roRef.current?.disconnect();
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[entries.length - 1]?.contentRect.width;
      if (w && w > 0) setWidth(Math.round(w));
    });
    ro.observe(el);
    roRef.current = ro;
  }, []);
  const PLOT_RIGHT = width - INSET_RIGHT;

  const header = (
    <div className={styles.head}>
      <div className={styles.title}>◆ Latency history</div>
      <div className={styles.legend}>
        {SERIES.map((s) => (
          <span key={s.key} className={styles.legendItem}>
            <span className={styles.swatch} style={{ background: `var(${s.token})` }} />
            {s.key}
          </span>
        ))}
      </div>
    </div>
  );

  // ── Loading ──────────────────────────────────────────────────────────────────
  if (isLoading) {
    return (
      <section className={styles.frame}>
        {header}
        <div className={styles.skeleton} aria-label="Loading latency history" />
      </section>
    );
  }

  // ── Error ────────────────────────────────────────────────────────────────────
  if (isError) {
    return (
      <section className={styles.frame}>
        {header}
        <div className={styles.errorRow}>
          <span className={styles.errorText}>Could not load latency history.</span>
          <button type="button" className={styles.retry} onClick={() => onRetry()}>
            Retry
          </button>
        </div>
      </section>
    );
  }

  const buckets: HistoryBucket[] =
    mode === 'drilldown' && drilldownSeries ? drilldownSeries : (history?.buckets ?? []);
  const n = buckets.length;
  const hasData = buckets.some((b) => b.count > 0);

  const xScale = linScale(0, Math.max(1, n - 1), PLOT_LEFT, PLOT_RIGHT);
  const maxVal = buckets.reduce((m, b) => Math.max(m, b.max_ms ?? 0), 0);
  const yMax = niceMax(maxVal);
  const yScale = linScale(0, yMax, PLOT_BOTTOM, PLOT_TOP);

  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(yMax * f));
  const xTicks = [0, 0.25, 0.5, 0.75, 1];

  const nowMs = history?.server_now_ms ?? (n > 0 ? buckets[n - 1].ts_ms : 0);

  const modeNote =
    mode === 'drilldown'
      ? `drill-down · ${drilldownLabel ?? ''} · client-computed`
      : filterActive
        ? 'Aggregate series — route/status filters apply to drill-down.'
        : null;

  function handleMove(e: React.MouseEvent<SVGSVGElement>) {
    if (!hasData || n === 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width === 0) return;
    const xPix = ((e.clientX - rect.left) / rect.width) * width;
    const t = (xPix - PLOT_LEFT) / (PLOT_RIGHT - PLOT_LEFT);
    setHoverIdx(Math.min(n - 1, Math.max(0, Math.round(t * (n - 1)))));
  }

  const hoverBucket = hoverIdx !== null ? buckets[hoverIdx] : null;

  return (
    <section className={styles.frame}>
      {header}
      {modeNote !== null && <div className={styles.modeNote}>{modeNote}</div>}
      <div className={styles.plotWrap} ref={measureRef}>
        <svg
          className={styles.chart}
          viewBox={`0 0 ${width} ${VIEWBOX_H}`}
          width="100%"
          preserveAspectRatio="none"
          role="img"
          aria-label="Latency history: p50, p95 and max over the selected window"
          onMouseMove={handleMove}
          onMouseLeave={() => setHoverIdx(null)}
        >
          {/* Horizontal gridlines + y-axis ms tick labels */}
          {yTicks.map((ms, i) => {
            const y = yScale(ms);
            return (
              <g key={`y${i}`}>
                <line
                  x1={PLOT_LEFT}
                  y1={y}
                  x2={PLOT_RIGHT}
                  y2={y}
                  stroke="var(--border)"
                  strokeWidth={0.5}
                />
                <text x={PLOT_LEFT - 4} y={y + 3} textAnchor="end" className={styles.tick}>
                  {ms.toLocaleString('en-US')}
                </text>
              </g>
            );
          })}

          {/* Minor vertical gridlines + x-axis relative-time tick labels */}
          {xTicks.map((f, i) => {
            const x = PLOT_LEFT + f * (PLOT_RIGHT - PLOT_LEFT);
            const label = f === 1 ? 'now' : `-${relTime(windowMs * (1 - f)).replace(/^-/, '')}`;
            return (
              <g key={`x${i}`}>
                <line
                  x1={x}
                  y1={PLOT_TOP}
                  x2={x}
                  y2={PLOT_BOTTOM}
                  stroke="var(--line)"
                  strokeWidth={0.5}
                />
                <text x={x} y={PLOT_BOTTOM + 12} textAnchor="middle" className={styles.tick}>
                  {label}
                </text>
              </g>
            );
          })}

          {hasData && (
            <>
              {/* Optional low-opacity p95 area (one path per non-null run) */}
              {polylinePoints(
                buckets.map((b) => b.p95_ms),
                xScale,
                yScale,
              ).map((run, i) => (
                <path key={`area${i}`} d={areaPath(run)} fill="var(--amber)" opacity={0.1} />
              ))}

              {/* Series polylines — one <polyline> per non-null run (null breaks the line) */}
              {SERIES.map((s) =>
                polylinePoints(buckets.map(s.pick), xScale, yScale).map((run, i) => (
                  <polyline
                    key={`${s.key}-${i}`}
                    points={run}
                    fill="none"
                    stroke={`var(${s.token})`}
                    strokeWidth={1.25}
                    data-series={s.key}
                  />
                )),
              )}

              {/* Hover guide line */}
              {hoverIdx !== null && (
                <line
                  x1={xScale(hoverIdx)}
                  y1={PLOT_TOP}
                  x2={xScale(hoverIdx)}
                  y2={PLOT_BOTTOM}
                  stroke="var(--faintest)"
                  strokeWidth={1}
                />
              )}
            </>
          )}
        </svg>

        {!hasData && (
          <p className={styles.emptyNote}>
            No requests in this window. Hit an ARM endpoint to watch latency react.
          </p>
        )}

        {hasData && hoverBucket !== null && hoverIdx !== null && (
          <div
            className={styles.tooltip}
            style={{ left: `${(xScale(hoverIdx) / width) * 100}%` }}
          >
            <div className={styles.ttTime}>{relTime(nowMs - hoverBucket.ts_ms)}</div>
            {hoverBucket.count > 0 ? (
              <>
                <div className={styles.ttCount}>
                  {hoverBucket.count.toLocaleString('en-US')} req
                </div>
                {SERIES.map((s) => (
                  <div key={s.key} className={styles.ttRow} style={{ color: `var(${s.token})` }}>
                    {s.key} {fmtMs(s.pick(hoverBucket))}
                  </div>
                ))}
              </>
            ) : (
              <div className={styles.ttMuted}>no traffic</div>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
