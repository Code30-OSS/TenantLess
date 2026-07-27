/**
 * ViewerView (S3 — DEMO-03, 18-UI-SPEC §S3) — the live ARM response viewer.
 *
 * DEMO-03 proves the runtime: the same synthetic tenant a scanner would hit, answered live. A curated
 * endpoint select (18-03 {@link RUNNABLE_ENDPOINTS}) + a `Run request` button compose the ARM path,
 * fire the live call through the EXISTING {@link armGet} (auto-Bearer, IAM-05 — no new fetch, no
 * control-token gate, D-03b), and render the response through the reused lazy {@link JsonTree} under a
 * `{method} · {path} · ●200 · {latency} ms` status strip.
 *
 * Real ids (D-03a) are sourced from the bearer-EXEMPT `/_sim` surface via the simGet-backed hooks:
 * `useSubscriptions` gives a real subscriptionId; a single tenant-wide `useResourceSearch` gives a
 * real full ARM id (+ its resource group) for the detail / list-in-group routes. Path composition
 * itself lives in `endpoints.ts` (reusing the unit-tested `queries.ts` builders) — this view carries
 * no path logic of its own and never constructs an absolute URL (armGet's `assertSameOrigin` covers it).
 *
 * Four-state contract (D-04): empty (pre-run `Ready when you are`, or an empty-tenant generate CTA when
 * `/_sim` reports 0 subscriptions), loading (`Running request…`), success (status strip + JsonTree),
 * error (the `ArmError` `{code} — {message}` in a `--red` row, with the prior successful body retained
 * beneath a muted note). The any-token gate means there is no auth-fail path.
 */
import { useRef, useState } from 'react';
import { Link } from 'react-router';

import { armGet, ArmError } from '../api/client';
import { useResourceSearch, useSubscriptions, useSummary } from '../api/queries';
import JsonTree from '../explorer/JsonTree';
import { RUNNABLE_ENDPOINTS } from './endpoints';
import styles from './ViewerView.module.css';

/** A completed live run — the client-measured round-trip the success strip + JsonTree render. */
interface RunResult {
  method: string;
  path: string;
  latencyMs: number;
  body: unknown;
}

export default function ViewerView() {
  const summary = useSummary();
  const subs = useSubscriptions();

  const [selectedId, setSelectedId] = useState(RUNNABLE_ENDPOINTS[0].id);
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<ArmError | null>(null);
  const [running, setRunning] = useState(false);
  // Retained prior successful body (Explorer's useRef-retained-prior pattern) — kept visible beneath
  // an error so a failed re-run does not blank the last good response.
  const lastGood = useRef<RunResult | null>(null);

  const selected = RUNNABLE_ENDPOINTS.find((e) => e.id === selectedId) ?? RUNNABLE_ENDPOINTS[0];

  // Real subscription id from the bearer-EXEMPT /_sim surface (D-03a).
  const sub = subs.data?.value?.[0]?.subscriptionId;
  // One tenant-wide resource-type search sources a real full ARM id (+ its rg) for the detail /
  // list-in-group routes. Gated so plain discovery routes fire no search (empty q => hook disabled).
  const wantsResource = selected.needsResId || selected.id === 'resources';
  const search = useResourceSearch({ q: wantsResource ? 'microsoft' : '', subscription: sub });
  const hit = search.data?.value?.[0];

  const ids = { sub, rg: hit?.resourceGroupName, armId: hit?.id };

  // Empty-tenant branch keys off the same /_sim/summary the topbar reads — 0 subs => generate CTA,
  // never a 404 from composing a path against ids that do not exist (Pitfall 3, T-18-08).
  const emptyTenant = summary.data?.totals?.subscriptions === 0;

  const idsReady =
    (!selected.needsSubId || Boolean(ids.sub)) &&
    (!selected.needsResId || Boolean(ids.armId)) &&
    (selected.id !== 'resources' || Boolean(ids.rg));

  const runDisabled = emptyTenant || running || !idsReady;

  async function run() {
    const path = selected.build(ids);
    setRunning(true);
    setError(null);
    const started = performance.now();
    try {
      const body = await armGet<unknown>(path);
      const done: RunResult = {
        method: selected.method,
        path,
        latencyMs: Math.round(performance.now() - started),
        body,
      };
      setResult(done);
      lastGood.current = done;
    } catch (err) {
      setError(err instanceof ArmError ? err : new ArmError('RequestFailed', String(err), 0));
    } finally {
      setRunning(false);
    }
  }

  return (
    <section className={styles.view} aria-label="Live response viewer">
      <header className={styles.head}>
        <div className={styles.eyebrow}>◆ LIVE RESPONSE VIEWER</div>
        <h1 className={styles.title}>Run a live ARM request</h1>
        <p className={styles.subtitle}>
          Send a real request to this tenant and inspect the ARM response. Authentication is
          automatic — no token needed.
        </p>
      </header>

      <div className={styles.controlRow}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor="viewer-endpoint">
            ENDPOINT
          </label>
          <select
            id="viewer-endpoint"
            className={styles.select}
            value={selectedId}
            onChange={(e) => {
              setSelectedId(e.target.value);
              setResult(null);
              setError(null);
            }}
          >
            {RUNNABLE_ENDPOINTS.map((ep) => (
              <option key={ep.id} value={ep.id}>
                {ep.label}
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          className={styles.primaryBtn}
          onClick={() => void run()}
          disabled={runDisabled}
        >
          Run request
        </button>
      </div>

      <div className={styles.result}>
        {emptyTenant ? (
          <div className={styles.empty}>
            <div className={styles.mark} aria-hidden="true">
              ◆
            </div>
            <h2 className={styles.emptyTitle}>No tenant loaded</h2>
            <p className={styles.emptyBody}>
              No tenant is loaded. Generate one from the Control Plane, then run a request.
            </p>
            <Link className={styles.ctaLink} to="/control-plane/generate">
              Go to the Control Plane
            </Link>
          </div>
        ) : running ? (
          <p className={styles.statusLine}>Running request…</p>
        ) : (
          <>
            {result && !error && (
              <>
                <div className={styles.statusStrip} data-testid="status-strip">
                  <span className={styles.metaText}>{result.method}</span>
                  <span className={styles.sep} aria-hidden="true">
                    ·
                  </span>
                  <span className={styles.metaPath}>{result.path}</span>
                  <span className={styles.sep} aria-hidden="true">
                    ·
                  </span>
                  <span className={styles.statusVal}>
                    <span
                      className={styles.dot}
                      style={{ background: 'var(--green)' }}
                      aria-hidden="true"
                    />
                    <span className={styles.metaText}>200</span>
                  </span>
                  <span className={styles.sep} aria-hidden="true">
                    ·
                  </span>
                  <span className={styles.metaText}>{result.latencyMs} ms</span>
                </div>
                <JsonTree data={result.body} rootLabel="response" />
              </>
            )}

            {error && (
              <>
                <div className={styles.errorRow} data-testid="error-row">
                  <span className={styles.errorText}>
                    {error.code} — {error.message}
                  </span>
                </div>
                {lastGood.current && (
                  <>
                    <p className={styles.mutedNote}>last successful response</p>
                    <JsonTree data={lastGood.current.body} rootLabel="response" />
                  </>
                )}
              </>
            )}

            {!result && !error && (
              <div className={styles.empty}>
                <div className={styles.mark} aria-hidden="true">
                  ◆
                </div>
                <h2 className={styles.emptyTitle}>Ready when you are</h2>
                <p className={styles.emptyBody}>
                  Pick an endpoint and press Run request to see a live ARM response from this tenant.
                </p>
              </div>
            )}
          </>
        )}
      </div>

      <p className={styles.footnote}>
        Authentication is automatic — a fixed non-empty token is sent for you; any non-empty token is
        accepted (IAM-05). No token entry needed.
      </p>
    </section>
  );
}
