/**
 * DependenciesView — the Explorer topology view (EXPL-04, `/ui/explorer/dependencies`).
 *
 * D-02: the mockup's D3 topology graph is DEFERRED — this ships the cross-subscription dependency
 * EDGE TABLE. It fetches `GET /_sim/dependencies` via `useDependencies` (bearer-EXEMPT `/_sim`, no
 * auth header — MOCK-09), feeds the paginated page to {@link DependencyTable} (gold cross-sub rows),
 * and offers a server-side filter bar (subscription matches source OR target · dependency type).
 *
 * Pagination is SERVER-SIDE (D-04 / T-15-22): `count` + the `$skiptoken` parsed off the previous
 * `nextLink` with `parseSkipToken`, re-requested against the UI's OWN relative path via the hook —
 * the absolute server-origin `nextLink` is never followed (Pitfall 3 / T-15-21). Every value renders
 * as JSX text (auto-escaped); no dangerouslySetInnerHTML (T-15-20).
 *
 * App.tsx already routes here — this replaces the 15-03 placeholder body only, not the route table.
 *
 * Filters apply DELIBERATELY (UAT Gap 6 / 15-12): the subscription/type inputs bind to a DRAFT; the
 * values fed to `useDependencies` come from the APPLIED snapshot, committed only on an explicit apply
 * (Apply button / Enter / blur). A cheap UUID-shape guard means an obviously-partial subscription is
 * never applied — no invalid-UUID request fires mid-keystroke.
 */
import { useState } from 'react';

import { useDependencies } from '../api/queries';
import { DEFAULT_TOP, parseSkipToken } from '../api/odata';
import DependencyTable from './DependencyTable';
import styles from './DependenciesView.module.css';

/** Fixed server page size we request — the pager range is computed against this, not the row count. */
const TOP = DEFAULT_TOP;

/** Canonical 8-4-4-4-12 hex UUID shape — a cheap client guard so a partial value is never applied. */
const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default function DependenciesView() {
  // DRAFT input state — typing updates ONLY the draft; it never reaches the query key (UAT Gap 6).
  const [subscription, setSubscription] = useState('');
  const [type, setType] = useState('');

  // APPLIED snapshot — the committed subscription/type. ONLY this feeds useDependencies.
  const [appliedSub, setAppliedSub] = useState('');
  const [appliedType, setAppliedType] = useState('');

  // Server-side keyset paging: the current cursor + the stack of prior cursors (for prev).
  const [skipToken, setSkipToken] = useState<string | null>(null);
  const [prevTokens, setPrevTokens] = useState<(string | null)[]>([]);

  const appliedSubTrim = appliedSub.trim();
  // Only a fully-shaped UUID is applied — an obviously-partial value is dropped, never requested.
  const subFilter = appliedSubTrim !== '' && UUID_SHAPE.test(appliedSubTrim) ? appliedSubTrim : undefined;
  const typeFilter = appliedType.trim() !== '' ? appliedType.trim() : undefined;
  const hasFilter = subFilter !== undefined || typeFilter !== undefined;
  // The applied subscription is non-empty but not a valid UUID — surface a hint instead of firing.
  const subShapeInvalid = appliedSubTrim !== '' && subFilter === undefined;

  const dirty = subscription !== appliedSub || type !== appliedType;

  const query = useDependencies({
    subscription: subFilter,
    type: typeFilter,
    top: TOP,
    skipToken: skipToken ?? undefined,
  });

  const rows = query.data?.value ?? [];
  const count = query.data?.count ?? 0;
  const nextLink = query.data?.nextLink;
  // Range against the FIXED page size (mirrors FilterBar) — the current page's row count is wrong on
  // a short final page (WR-04). pageIndex is the length of the prior-cursor stack.
  const pageStart = rows.length > 0 ? prevTokens.length * TOP + 1 : 0;

  function resetPaging() {
    setSkipToken(null);
    setPrevTokens([]);
  }

  // Commit the draft -> applied snapshot. This is the ONLY path a filter edit reaches the query key;
  // applying starts a fresh result at page 1 (resetPaging). A partial UUID is applied here but the
  // shape guard above keeps it out of the actual request.
  function apply() {
    setAppliedSub(subscription);
    setAppliedType(type);
    resetPaging();
  }

  function clear() {
    setSubscription('');
    setType('');
    setAppliedSub('');
    setAppliedType('');
    resetPaging();
  }

  function goNext() {
    const token = parseSkipToken(nextLink);
    if (!token) return;
    setPrevTokens((stack) => [...stack, skipToken]);
    setSkipToken(token);
  }

  function goPrev() {
    // Pure transition (WR-03): derive the prior cursor + trimmed stack from the CURRENT state and
    // issue two independent, side-effect-free setters — no setState nested in another's updater.
    if (prevTokens.length === 0) return;
    const prior = prevTokens[prevTokens.length - 1];
    setPrevTokens(prevTokens.slice(0, -1));
    setSkipToken(prior);
  }

  return (
    <section data-testid="dependencies-view" aria-label="Cross-subscription dependencies" className={styles.view}>
      <header className={styles.head}>
        <div className={styles.eyebrow}>◆ Tenant explorer · topology</div>
        <h1 className={styles.title}>Cross-subscription dependencies</h1>
        <p className={styles.framing}>
          Typed edges between resources. <span className={styles.gold}>Gold edges cross subscription
          boundaries</span> — the blast-radius surface.
        </p>
      </header>

      <div className={styles.filterBar}>
        <label className={styles.field}>
          <span className={styles.label}>subscription</span>
          <input
            className={styles.input}
            aria-label="subscription"
            value={subscription}
            placeholder="b7e2-…-1c4a (source or target)"
            onChange={(e) => setSubscription(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') apply();
            }}
            onBlur={apply}
          />
        </label>
        <label className={styles.field}>
          <span className={styles.label}>type</span>
          <input
            className={styles.input}
            aria-label="type"
            value={type}
            placeholder="private-endpoint"
            onChange={(e) => setType(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') apply();
            }}
            onBlur={apply}
          />
        </label>
        <button type="button" className={styles.clear} onClick={apply} disabled={!dirty}>
          Apply filter
        </button>
        {(hasFilter || subShapeInvalid) && (
          <button type="button" className={styles.clear} onClick={clear}>
            Clear filter
          </button>
        )}
        {subShapeInvalid && (
          <span className={styles.count} data-testid="dep-sub-hint">
            enter a full subscription id
          </span>
        )}
        <span className={styles.count} data-testid="dep-count">
          {count} edge{count === 1 ? '' : 's'}
        </span>
      </div>

      {query.isError ? (
        <div className={styles.errorRow}>
          <span className={styles.errorText}>Could not load dependencies.</span>
          <button type="button" className={styles.retry} onClick={() => query.refetch()}>
            Retry
          </button>
        </div>
      ) : query.isLoading ? (
        <div className={styles.skeletonWrap} aria-label="Loading dependencies">
          <div className={styles.skeleton} />
          <div className={styles.skeleton} />
          <div className={styles.skeleton} />
        </div>
      ) : rows.length === 0 ? (
        hasFilter ? (
          <div className={styles.emptyRow}>
            <span className={styles.emptyText}>No dependencies match this filter.</span>
            <button type="button" className={styles.clear} onClick={clear}>
              Clear filter
            </button>
          </div>
        ) : (
          <div className={styles.emptyRow}>
            <span className={styles.emptyText}>This tenant has no cross-resource dependencies.</span>
          </div>
        )
      ) : (
        <>
          <DependencyTable edges={rows} />
          <div className={styles.pager}>
            <span className={styles.range} data-testid="dep-range">
              {pageStart}–{pageStart + rows.length - 1} of {count}
            </span>
            <button
              type="button"
              className={styles.prev}
              onClick={goPrev}
              disabled={prevTokens.length === 0}
            >
              prev
            </button>
            <button type="button" className={styles.next} onClick={goNext} disabled={!nextLink}>
              next ›
            </button>
          </div>
        </>
      )}
    </section>
  );
}
