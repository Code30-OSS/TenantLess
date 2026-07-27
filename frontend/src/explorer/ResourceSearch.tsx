/**
 * ResourceSearch (15-14, EXPL-01/EXPL-05) — the tenant-wide, SERVER-SIDE resource search box.
 *
 * The Explorer tree drills in but cannot FIND a resource by name across a 100K+ tenant, and the
 * FilterBar only does ARM `$filter` scoped to a selected RG. This box searches the WHOLE tenant by
 * name OR type substring via the bearer-exempt `GET /_sim/resources/search` (name/type ILIKE,
 * keyset-paginated) — the user chose server-side over loading the tenant client-side.
 *
 * Draft/applied discipline (UAT Gap 6 parity, 15-12): typing updates a DRAFT; only an explicit
 * commit (Enter / the Search button) promotes it to the `applied` term that reaches the query key —
 * so NO request fires per keystroke. Results render as a FLAT, self-contained paginated list using
 * the SAME bounded Prev/Next replace idiom as the tree (one page at a time inside a height-capped
 * scroll region — the panel never grows unbounded). The `$skiptoken` is parsed off the UI's OWN
 * previous `nextLink` and re-requested via the relative builder — never the absolute origin
 * (T-15-17/21). Selecting a hit raises `onSelectResource({ armId, sub, rg })`, which the view lifts
 * to the detail panel + `?sub&rg&res` URL exactly like a tree/FilterBar selection. All rendered
 * values are JSX text (auto-escaped) — no `dangerouslySetInnerHTML` (T-15-19).
 */
import { useState } from 'react';

import { useResourceSearch } from '../api/queries';
import { parseSkipToken } from '../api/odata';
import type { TreeSelection } from './ResourceTree';
import { shortType } from './ResourceTree';
import styles from './ResourceSearch.module.css';

interface ResourceSearchProps {
  /** The currently selected resource id (highlights its row in the result list). */
  selectedResId: string | null;
  /** Raised when a search-result row is clicked (the shared select() → ?sub&rg&res). */
  onSelectResource: (sel: TreeSelection) => void;
  /**
   * Raised when a subscription-name match is clicked (EXPL-GAP-01): selects that subscription in
   * Miller col1 (the view sets `?sub` and clears `?rg`/`?res`). Wired once against the 15-16 col1.
   */
  onSelectSubscription: (subscriptionId: string) => void;
  /**
   * Raised when a resource-group-name match is clicked (RG-name search): selects that RG in the
   * Miller columns (the view sets `?sub&rg` and clears `?res`, like a col1 RG click). This is how
   * you find `rg-corp-dev-backup-43` among thousands of RGs — resources aren't named like their RGs.
   */
  onSelectResourceGroup: (subscriptionId: string, resourceGroup: string) => void;
}

export default function ResourceSearch({
  selectedResId,
  onSelectResource,
  onSelectSubscription,
  onSelectResourceGroup,
}: ResourceSearchProps) {
  // DRAFT input — typing updates ONLY this; it never reaches the query key (no per-keystroke fetch).
  const [draft, setDraft] = useState('');
  // APPLIED term — the committed search. ONLY this drives useResourceSearch.
  const [applied, setApplied] = useState('');

  // Bounded Prev/Next paging: the current cursor + the stack of prior cursors (replace, never append).
  const [skipToken, setSkipToken] = useState<string | null>(null);
  const [prevTokens, setPrevTokens] = useState<(string | null)[]>([]);

  const query = useResourceSearch({ q: applied, skipToken: skipToken ?? undefined });
  const rows = query.data?.value ?? [];
  // Subscription-NAME matches (EXPL-GAP-01) — bounded, name-ASC; `[]` when no sub name matches.
  const subs = query.data?.subscriptions ?? [];
  // Resource-group-NAME matches (RG-name search) — bounded, name-ASC; `[]` when no RG name matches.
  const rgs = query.data?.resourceGroups ?? [];
  const nextLink = query.data?.nextLink;
  const count = query.data?.count ?? 0;
  const hasApplied = applied.trim() !== '';

  function apply() {
    setApplied(draft);
    // A fresh search starts at page 1.
    setSkipToken(null);
    setPrevTokens([]);
  }

  function goNext() {
    const token = parseSkipToken(nextLink);
    if (!token) return;
    setPrevTokens((stack) => [...stack, skipToken]);
    setSkipToken(token);
  }

  function goPrev() {
    // Pure two-setter transition (WR-03): pop the last prior cursor off the stack.
    if (prevTokens.length === 0) return;
    const prior = prevTokens[prevTokens.length - 1];
    setPrevTokens(prevTokens.slice(0, -1));
    setSkipToken(prior);
  }

  const hasPrev = prevTokens.length > 0;

  return (
    <section className={styles.search} aria-label="Resource search">
      <div className={styles.controls}>
        <label className={styles.field}>
          <span className={styles.label}>search</span>
          <input
            className={styles.input}
            type="search"
            aria-label="Search resources"
            value={draft}
            placeholder="Find a resource by name or type across the whole tenant…"
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') apply();
            }}
          />
        </label>
        <button type="button" className={styles.apply} onClick={apply}>
          Search
        </button>
      </div>

      <p className={styles.note}>Searches the whole tenant (server-side) by name or type substring.</p>

      {/* Subscription-NAME matches (EXPL-GAP-01): rendered ABOVE the resource results, and ONLY when
          a term is applied AND the backend returned a non-empty `subscriptions` array (no empty header).
          A row click selects that subscription in Miller col1 via onSelectSubscription (the view sets
          ?sub, clears ?rg/?res). Names are JSX text (auto-escaped) — no dangerouslySetInnerHTML. */}
      {hasApplied && subs.length > 0 && (
        <section className={styles.subsSection} aria-label="Subscription matches">
          <div className={styles.sectionLabel}>Subscriptions</div>
          <ul className={styles.list}>
            {subs.map((s) => (
              <li key={s.id}>
                <button
                  type="button"
                  className={styles.resRow}
                  onClick={() => onSelectSubscription(s.id)}
                >
                  <span className={styles.name}>{s.name}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Resource-group-NAME matches (RG-name search): rendered as its own section, ONLY when a
          term is applied AND the backend returned a non-empty `resourceGroups` array. A row click
          selects that RG in the Miller columns via onSelectResourceGroup (the view sets ?sub&rg,
          clears ?res). Names are JSX text (auto-escaped) — no dangerouslySetInnerHTML. The
          `${subscriptionId}/${name}` key disambiguates a name shared across subscriptions. */}
      {hasApplied && rgs.length > 0 && (
        <section className={styles.subsSection} aria-label="Resource group matches">
          <div className={styles.sectionLabel}>Resource groups</div>
          <ul className={styles.list}>
            {rgs.map((g) => (
              <li key={`${g.subscriptionId}/${g.name}`}>
                <button
                  type="button"
                  className={styles.resRow}
                  onClick={() => onSelectResourceGroup(g.subscriptionId, g.name)}
                >
                  <span className={styles.name}>{g.name}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {!hasApplied ? (
        <p className={styles.hint}>Type a term and press Enter to search across every subscription.</p>
      ) : query.isLoading && rows.length === 0 ? (
        <div aria-label="Loading search results">
          <div className={styles.skeleton} />
          <div className={styles.skeleton} />
        </div>
      ) : query.isError ? (
        <div className={styles.errorRow}>
          <span className={styles.errorText}>
            Search failed: {query.error?.message ?? 'unknown error'}
          </span>
        </div>
      ) : rows.length === 0 ? (
        <p className={styles.hint}>
          No resources match <code className={styles.code}>{applied}</code>.
        </p>
      ) : (
        <>
          <div className={styles.resultsHead}>
            <span className={styles.count}>{count} match{count === 1 ? '' : 'es'}</span>
          </div>
          {/* Height-capped, internally-scrolling region so the result list never grows the page. */}
          <div className={styles.scroll} data-testid="search-scroll">
            <ul className={styles.list}>
              {rows.map((r) => (
                <li key={r.id}>
                  <button
                    type="button"
                    className={styles.resRow}
                    data-selected={r.id === selectedResId}
                    onClick={() =>
                      onSelectResource({ armId: r.id, sub: r.subscriptionId, rg: r.resourceGroupName })
                    }
                  >
                    <span className={styles.name}>{r.name}</span>
                    <span className={styles.resType}>{shortType(r.type)}</span>
                    <span className={styles.scope}>{r.resourceGroupName}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
          <div className={styles.pager}>
            <span className={styles.pageReadout}>page {prevTokens.length + 1}</span>
            <button
              type="button"
              className={styles.pageBtn}
              aria-label="Previous results"
              onClick={goPrev}
              disabled={!hasPrev}
            >
              ‹ prev
            </button>
            <button
              type="button"
              className={styles.pageBtn}
              aria-label="Next results"
              onClick={goNext}
              disabled={!nextLink}
            >
              next ›
            </button>
          </div>
        </>
      )}
    </section>
  );
}
