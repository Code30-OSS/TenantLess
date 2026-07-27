/**
 * RequestInspector — the CONS-03 request detail panel. The right-column analog of the Explorer's
 * `ResourceDetail`: an empty-selection placeholder, then a copyable detail panel over ONLY the six
 * captured `RequestEvent` fields (D-06 — no headers/query/bodies were ever captured, so none are
 * invented here). There is deliberately NO loading/error state: the selected event is already in the
 * client buffer, so there is nothing to fetch.
 *
 * Color discipline: the status dot reads the shared `STATUS_TOKEN` map (`api/console.ts`, sole owner)
 * keyed by `statusClass` (`console/filter.ts`, sole owner) — a `var(--token)` string, never a raw hex.
 *
 * Security (T-16-03): every field reaches the DOM as auto-escaped JSX text and `Copy event` copies a
 * plain JSON serialization via `navigator.clipboard.writeText` — there is no raw-HTML injection sink,
 * so a scanner-injected `path`/`route` (e.g. an `<img onerror>`) renders inert and never executes.
 */
import type { ReactNode } from 'react';

import { STATUS_TOKEN, type RequestEvent } from '../api/console';
import { statusClass } from './filter';
import styles from './RequestInspector.module.css';

interface RequestInspectorProps {
  /** The event open in the inspector, or null → placeholder. */
  selected: RequestEvent | null;
  /** Injectable clock for the relative `ts_ms` suffix (defaults to `Date.now()`). */
  nowMs?: number;
}

/** Absolute local wall-clock time (24h) for the `ts_ms` field. */
function absoluteTs(tsMs: number): string {
  return new Date(tsMs).toLocaleTimeString('en-GB', { hour12: false });
}

/** Compact relative age, e.g. `-12s` / `-3m` (clamped at 0). */
function relativeTs(tsMs: number, nowMs: number): string {
  const secs = Math.max(0, Math.round((nowMs - tsMs) / 1000));
  if (secs < 60) return `-${secs}s`;
  return `-${Math.floor(secs / 60)}m`;
}

export default function RequestInspector({ selected, nowMs = Date.now() }: RequestInspectorProps) {
  if (!selected) {
    return (
      <div className={styles.panel}>
        <p className={styles.emptyState}>Select a request from the feed to inspect it.</p>
      </div>
    );
  }

  const { method, path, route, status, latency_ms, ts_ms } = selected;
  const token = STATUS_TOKEN[statusClass(status)];

  function copyEvent() {
    // Six fields only (D-06), in a stable order; a plain JSON string — never an HTML sink.
    void navigator.clipboard.writeText(
      JSON.stringify({ method, path, route, status, latency_ms, ts_ms }),
    );
  }

  return (
    <div className={styles.panel}>
      <dl className={styles.meta}>
        <MetaRow field="method" label="method">
          <span className={styles.value}>{method}</span>
        </MetaRow>
        <MetaRow field="path" label="path">
          <span className={`${styles.value} ${styles.break}`}>{path}</span>
        </MetaRow>
        <MetaRow field="route" label="route">
          <span className={`${styles.value} ${styles.muted}`}>{route}</span>
        </MetaRow>
        <MetaRow field="status" label="status">
          <span className={styles.statusVal}>
            <span className={styles.dot} style={{ background: `var(${token})` }} aria-hidden="true" />
            <span className={styles.value}>{status}</span>
          </span>
        </MetaRow>
        <MetaRow field="latency_ms" label="latency_ms">
          <span className={styles.value}>{latency_ms} ms</span>
        </MetaRow>
        <MetaRow field="ts_ms" label="ts_ms">
          <span className={styles.value}>
            {absoluteTs(ts_ms)} · {relativeTs(ts_ms, nowMs)}
          </span>
        </MetaRow>
      </dl>

      <button type="button" className={styles.copyBtn} onClick={copyEvent}>
        Copy event
      </button>
    </div>
  );
}

/** One `dt`/`dd` field row (the `ResourceDetail.MetaRow` idiom); `data-field` marks it for the tests. */
function MetaRow({ field, label, children }: { field: string; label: string; children: ReactNode }) {
  return (
    <div className={styles.metaRow} data-field={field}>
      <dt className={styles.metaKey}>{label}</dt>
      <dd className={styles.metaValue}>{children}</dd>
    </div>
  );
}
