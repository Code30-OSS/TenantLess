/**
 * FeedTable — the CONS-04 live request feed. A presentational, newest-first request table reusing the
 * shipped `DependencyTable` layout idiom (sticky `--panel-hi` header, `--line` row dividers, hover
 * `--panel-hi`, the inline gold selected-row accent). Data arrives already newest-first from the live
 * buffer (16-03 hook) — this component fetches nothing and owns no filter state.
 *
 * Color discipline: the status number is tinted via the shared `STATUS_TOKEN` map (`api/console.ts`,
 * the sole owner) keyed by `statusClass` (`console/filter.ts`, the sole owner of the band classifier) —
 * always a `var(--token)` string, never a raw hex. The selected row (identity match on the event)
 * carries the same `inset 2px 0 0 var(--gold)` accent used by `DependencyTable`/`FilterBar .resRow`.
 *
 * Motion (UI-SPEC): a freshly-prepended row runs a ONE-SHOT `pulse` gold left-accent then settles —
 * tracked via a `seen` ref (seeded from the first buffer so the initial paint does not pulse every
 * seeded row) and guarded by `prefers-reduced-motion` in the CSS Module.
 *
 * Security (T-16-03): the scanner-controlled `path`/`route` reach the DOM ONLY as auto-escaped JSX
 * text — no raw-HTML injection sink anywhere; a malicious `path` renders inert.
 */
import { useEffect, useRef } from 'react';

import { STATUS_TOKEN, type RequestEvent } from '../api/console';
import { statusClass } from './filter';
import styles from './FeedTable.module.css';

/** The SSE connection lifecycle the feed reflects (owned by `useEventStream`, 16-03). */
export type FeedState = 'connecting' | 'live' | 'reconnecting';
/** Which empty state to show when there are no rows: never-any-traffic vs the filter excluded all. */
export type FeedEmptyKind = 'no-traffic' | 'filtered';

interface FeedTableProps {
  /** Events already ordered newest-first by the buffer. */
  events: RequestEvent[];
  /** The event currently open in the inspector (identity match), or null. */
  selected: RequestEvent | null;
  /** Raised with the clicked row's event. */
  onSelect: (ev: RequestEvent) => void;
  /** SSE lifecycle — drives the connecting skeleton + reconnecting banner. */
  state: FeedState;
  /** Distinguishes the two empty-state copy strings. */
  emptyKind: FeedEmptyKind;
  /** Injectable clock for the relative `ts` column (defaults to `Date.now()`). */
  nowMs?: number;
}

/** Column header labels, left→right (also the `data-col` keys used by the tests). */
const COLUMNS = ['method', 'path', 'route', 'status', 'ms', 'ts'] as const;

/** A stable-ish row key from the event shape (events carry no unique id). */
function eventKey(ev: RequestEvent): string {
  return `${ev.ts_ms}:${ev.method}:${ev.path}:${ev.status}`;
}

/** Compact relative age of an event, e.g. `-12s` / `-3m` (clamped at 0). */
function relativeTs(tsMs: number, nowMs: number): string {
  const secs = Math.max(0, Math.round((nowMs - tsMs) / 1000));
  if (secs < 60) return `-${secs}s`;
  return `-${Math.floor(secs / 60)}m`;
}

export default function FeedTable({
  events,
  selected,
  onSelect,
  state,
  emptyKind,
  nowMs = Date.now(),
}: FeedTableProps) {
  // Track which rows we've already shown so ONLY a freshly-prepended row runs the one-shot `pulse`
  // gold accent. Seeded lazily from the initial buffer so the first paint does not pulse every row.
  const seenRef = useRef<Set<string> | null>(null);
  if (seenRef.current === null) {
    seenRef.current = new Set(events.map(eventKey));
  }
  const seen = seenRef.current;

  useEffect(() => {
    const s = seenRef.current;
    if (s) for (const ev of events) s.add(eventKey(ev));
  }, [events]);

  if (state === 'connecting') {
    return (
      <div className={styles.wrap}>
        <div className={styles.skeleton} aria-label="Connecting to live feed" />
        <div className={styles.skeleton} />
        <div className={styles.skeleton} />
      </div>
    );
  }

  return (
    <div className={styles.wrap}>
      {state === 'reconnecting' && (
        <div className={styles.reconnecting} role="status">
          Live feed disconnected — reconnecting…
        </div>
      )}

      {events.length === 0 ? (
        <p className={styles.empty}>
          {emptyKind === 'filtered'
            ? 'No requests match this filter.'
            : 'Waiting for requests — the feed updates as the mock serves ARM traffic.'}
        </p>
      ) : (
        <div className={styles.scroll}>
          <table className={styles.table}>
            <thead>
              <tr>
                {COLUMNS.map((c) => (
                  <th key={c} className={styles.th}>
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {events.map((ev) => {
                const key = eventKey(ev);
                const isSelected = ev === selected;
                const isFresh = !seen.has(key);
                const token = STATUS_TOKEN[statusClass(ev.status)];
                const rowClass = [styles.row, isFresh ? styles.rowNew : ''].filter(Boolean).join(' ');
                return (
                  <tr
                    key={key}
                    data-event-key={key}
                    data-selected={isSelected || undefined}
                    className={rowClass}
                    // Inline gold accent (token — assertable + raw-hex-free), mirroring DependencyTable.
                    style={isSelected ? { boxShadow: 'inset 2px 0 0 var(--gold)' } : undefined}
                    role="button"
                    tabIndex={0}
                    onClick={() => onSelect(ev)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        onSelect(ev);
                      }
                    }}
                  >
                    <td data-col="method" className={styles.method}>
                      {ev.method}
                    </td>
                    <td data-col="path" className={styles.path} title={ev.path}>
                      {ev.path}
                    </td>
                    <td data-col="route" className={styles.route} title={ev.route}>
                      {ev.route}
                    </td>
                    <td data-col="status" className={styles.status} style={{ color: `var(${token})` }}>
                      {ev.status}
                    </td>
                    <td data-col="ms" className={styles.ms}>
                      {ev.latency_ms}
                    </td>
                    <td data-col="ts" className={styles.ts}>
                      {relativeTs(ev.ts_ms, nowMs)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
