/**
 * ResourcesView — the default Explorer view (EXPL-01, `/ui/explorer/resources`).
 *
 * Composes the KPI header (three `totals` from `/_sim/summary`), a drill-down breadcrumb, the tenant
 * search + filter bars, and the 3-pane MILLER-COLUMN body (EXPL-GAP-02): col1 {@link SubscriptionColumn}
 * (subscriptions/RGs) | col2 {@link ResourceColumn} (the selected RG's resources) | col3
 * {@link ResourceDetail}. Each column scrolls independently. Selection is lifted here and mirrored to
 * the URL query (`?sub=&rg=&res=`) via `useSearchParams`, so it survives reload and restores all three
 * panes: an RG selection sets `?sub&rg` and CLEARS `?res` (col2 reloads, col3 empties); a resource
 * selection (from col2, search, or the filter bar) sets `?sub&rg&res` (col3 shows detail).
 *
 * App.tsx already routes here — this plan replaces the body layout only, not the route table.
 */
import { useState } from 'react';
import { useSearchParams } from 'react-router';

import { useSummary } from '../api/queries';
import FilterBar from './FilterBar';
import KpiStat from './KpiStat';
import ResourceDetail from './ResourceDetail';
import ResourceSearch from './ResourceSearch';
import {
  ResourceColumn,
  SubscriptionColumn,
  type RgSelection,
  type TreeSelection,
} from './ResourceTree';
import styles from './ResourcesView.module.css';

export default function ResourcesView() {
  const [params, setParams] = useSearchParams();
  const sub = params.get('sub');
  const rg = params.get('rg');
  const res = params.get('res');

  // Applied OData `$filter` (EXPL-05) lifted from the FilterBar controls and threaded into col2 —
  // the FilterBar no longer renders its own (duplicate) list post-Miller; col2 owns the resource list.
  const [filter, setFilter] = useState<string | undefined>(undefined);

  const { data: summary } = useSummary();
  const totals = summary?.totals;
  const subName = summary?.subscriptions.find((s) => s.subscriptionId === sub)?.name ?? sub;
  const resName = res ? (res.split('/').pop() ?? res) : null;

  // Resource selection (col2 / search / filter): fills col3 — sets sub+rg+res.
  const select = (sel: TreeSelection) => {
    const next = new URLSearchParams(params);
    next.set('sub', sel.sub);
    next.set('rg', sel.rg);
    next.set('res', sel.armId);
    setParams(next);
  };

  // Resource-group selection (col1): fills col2 and CLEARS col3 — sets sub+rg, deletes res.
  const onSelectRg = (sel: RgSelection) => {
    const next = new URLSearchParams(params);
    next.set('sub', sel.sub);
    next.set('rg', sel.rg);
    next.delete('res');
    setParams(next);
  };

  // Subscription selection (search Subscriptions match, EXPL-GAP-01): selects it in Miller col1 —
  // sets ?sub and CLEARS ?rg/?res, so SubscriptionColumn marks it selected and expands it in col1
  // both on a fresh deep-link mount AND on an in-session click (SubRow syncs `open` to the new
  // `initialSub` via a useEffect true-transition, WR-01 — not just the once-only useState seed).
  const selectSubscription = (subscriptionId: string) => {
    const next = new URLSearchParams(params);
    next.set('sub', subscriptionId);
    next.delete('rg');
    next.delete('res');
    setParams(next);
  };

  return (
    <section data-testid="resources-view" aria-label="Resource tree" className={styles.view}>
      <header className={styles.head}>
        <div className={styles.eyebrow}>◆ Tenant explorer</div>
        <h1 className={styles.title}>Resource tree</h1>
        <div className={styles.kpis}>
          <KpiStat value={totals?.resources ?? '—'} label="resources" />
          <KpiStat value={totals?.resourceGroups ?? '—'} label="resource groups" />
          <KpiStat value={totals?.subscriptions ?? '—'} label="subscriptions" />
        </div>
      </header>

      <nav className={styles.breadcrumb} aria-label="Breadcrumb">
        {sub ? (
          <>
            <span className={styles.crumb}>{subName}</span>
            {rg && (
              <>
                <span className={styles.sep} aria-hidden="true">
                  /
                </span>
                <span className={styles.crumb}>{rg}</span>
              </>
            )}
            {resName && (
              <>
                <span className={styles.sep} aria-hidden="true">
                  /
                </span>
                <span className={styles.crumbActive}>{resName}</span>
              </>
            )}
          </>
        ) : (
          <span className={styles.crumbMuted}>Select a resource to drill down.</span>
        )}
      </nav>

      {/* Tenant-wide search (15-14): server-side name/type substring across the WHOLE tenant,
          deep-linking a hit into the detail panel exactly like a tree/FilterBar selection. */}
      <ResourceSearch
        onSelectResource={select}
        onSelectSubscription={selectSubscription}
        onSelectResourceGroup={(subscriptionId, resourceGroup) =>
          onSelectRg({ sub: subscriptionId, rg: resourceGroup })
        }
        selectedResId={res}
      />

      {/* $filter CONTROLS (EXPL-05): server-side type/location/tag filter — lifts the applied
          `$filter` into col2 below (no duplicate list here post-Miller). */}
      <FilterBar sub={sub} rg={rg} onApply={setFilter} />

      <div className={styles.body}>
        <div className={styles.col1}>
          <SubscriptionColumn
            selectedSub={sub}
            selectedRg={rg}
            initialSub={sub}
            initialRg={rg}
            onSelectRg={onSelectRg}
          />
        </div>
        <div className={styles.col2}>
          <ResourceColumn sub={sub} rg={rg} filter={filter} selectedResId={res} onSelectResource={select} />
        </div>
        <div className={styles.detailCol}>
          <ResourceDetail armId={res} />
        </div>
      </div>
    </section>
  );
}
