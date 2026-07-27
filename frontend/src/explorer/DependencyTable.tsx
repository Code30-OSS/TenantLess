/**
 * DependencyTable — the EXPL-04 cross-subscription dependency EDGE TABLE (D-02).
 *
 * D-02 override: the mockup's SVG/D3 topology graph is DEFERRED — this is a sortable, filterable
 * edge TABLE (no graph/viz library). It renders typed `source → target` edges with the dependency
 * type and a cross-sub badge; **cross-subscription rows are gold-accented** (`var(--gold)`) — the
 * blast-radius signal carried over from the graph.
 *
 * Presentational + pure: it takes an `edges` array (a single server-paginated page; fetching +
 * filtering + paging live in DependenciesView) and owns only its client-side SORT state over the
 * current page. Every cell value is rendered as JSX text (auto-escaped) — no dangerouslySetInnerHTML
 * (T-15-20). Sorting one page client-side keeps the server the paging authority (T-15-22).
 */
import { useMemo, useState } from 'react';

import type { Dependency } from '../api/types';
import styles from './DependencyTable.module.css';

/** The sortable columns, each mapped to a comparator over the edge shape. */
export type SortKey = 'source' | 'target' | 'type' | 'crossSubscription';
export type SortDir = 'asc' | 'desc';

/** Comparator set the sortable headers toggle over (ascending; `desc` reverses). */
const COMPARATORS: Record<SortKey, (a: Dependency, b: Dependency) => number> = {
  source: (a, b) => a.source.resourceId.localeCompare(b.source.resourceId),
  target: (a, b) => a.target.resourceId.localeCompare(b.target.resourceId),
  type: (a, b) => a.type.localeCompare(b.type),
  crossSubscription: (a, b) => Number(a.crossSubscription) - Number(b.crossSubscription),
};

interface DependencyTableProps {
  /** One server-paginated page of edges (already filtered server-side). */
  edges: Dependency[];
}

export default function DependencyTable({ edges }: DependencyTableProps) {
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>('asc');

  const sorted = useMemo(() => {
    if (!sortKey) return edges;
    const arr = [...edges].sort(COMPARATORS[sortKey]);
    return sortDir === 'desc' ? arr.reverse() : arr;
  }, [edges, sortKey, sortDir]);

  // Distinct dependency types present on this page (for the legend edge-type list).
  const types = useMemo(
    () => Array.from(new Set(edges.map((e) => e.type))).sort((a, b) => a.localeCompare(b)),
    [edges],
  );

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  }

  return (
    <div className={styles.wrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            <SortHeader label="Source" col="source" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
            <th className={styles.arrowHead} aria-hidden="true" />
            <SortHeader label="Target" col="target" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
            <SortHeader label="Type" col="type" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
            <SortHeader
              label="Cross-sub"
              col="crossSubscription"
              sortKey={sortKey}
              sortDir={sortDir}
              onSort={toggleSort}
            />
          </tr>
        </thead>
        <tbody>
          {sorted.map((e) => (
            <tr
              key={edgeKey(e)}
              data-cross-sub={e.crossSubscription}
              className={styles.row}
              // Gold left-accent for cross-sub edges — the blast-radius signal (D-02). Inline so the
              // token reference is assertable + raw-hex-free (CSS Modules hash class names in vitest).
              style={e.crossSubscription ? { boxShadow: 'inset 3px 0 0 var(--gold)' } : undefined}
            >
              <td className={styles.endpoint}>
                <Endpoint resourceId={e.source.resourceId} subscriptionId={e.source.subscriptionId} />
              </td>
              <td className={styles.arrow} aria-hidden="true">
                →
              </td>
              <td className={styles.endpoint}>
                <Endpoint resourceId={e.target.resourceId} subscriptionId={e.target.subscriptionId} />
              </td>
              <td className={styles.type}>{e.type}</td>
              <td className={styles.crossCell}>
                {e.crossSubscription ? (
                  <span className={styles.crossBadge}>● cross-sub</span>
                ) : (
                  <span className={styles.sameBadge}>same-sub</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className={styles.legend}>
        <span className={styles.legendCross}>● cross-subscription edge</span>
        {types.length > 0 && (
          <span className={styles.legendTypes}>
            {' · '}
            {types.join(' · ')}
          </span>
        )}
      </div>
    </div>
  );
}

/** A source/target endpoint cell: the resource name (last ARM segment) over its subscription id. */
function Endpoint({ resourceId, subscriptionId }: { resourceId: string; subscriptionId: string }) {
  return (
    <span className={styles.endpointInner} title={resourceId}>
      <span className={styles.resName}>{resourceName(resourceId)}</span>
      <span className={styles.subId}>{subscriptionId}</span>
    </span>
  );
}

interface SortHeaderProps {
  label: string;
  col: SortKey;
  sortKey: SortKey | null;
  sortDir: SortDir;
  onSort: (col: SortKey) => void;
}

/** A clickable, sortable column header showing the asc/desc arrow when it is the active sort. */
function SortHeader({ label, col, sortKey, sortDir, onSort }: SortHeaderProps) {
  const active = sortKey === col;
  const ariaSort = active ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none';
  return (
    <th className={styles.th} aria-sort={ariaSort}>
      <button type="button" className={styles.sortBtn} data-active={active} onClick={() => onSort(col)}>
        <span>{label}</span>
        <span className={styles.caret} aria-hidden="true">
          {active ? (sortDir === 'asc' ? '▲' : '▼') : '↕'}
        </span>
      </button>
    </th>
  );
}

/** Last path segment of an ARM resource id (the resource's own name). */
function resourceName(resourceId: string): string {
  const trimmed = resourceId.replace(/\/+$/, '');
  const seg = trimmed.split('/').pop();
  return seg && seg.length > 0 ? seg : resourceId;
}

/** A stable-ish row key from the edge triple (edges carry no unique id). */
function edgeKey(e: Dependency): string {
  return `${e.source.resourceId}→${e.target.resourceId}:${e.type}`;
}
