/**
 * ResourceTree — the two left panes of the EXPL-GAP-02 Miller-column Explorer:
 *
 *  - {@link SubscriptionColumn} (col1): the subscription list. A subscription row EXPANDS its resource
 *    groups inline (lazy — the RG list mounts only on expand, so `useResourceGroups` fires only then).
 *    An RG row is a SELECT button: clicking it raises `onSelectRg({ sub, rg })` and marks that RG row
 *    (gold inset). RGs no longer nest a resource list inside col1 — the selected RG drives col2 instead.
 *  - {@link ResourceColumn} (col2): a muted prompt until an RG is selected; once `{ sub, rg }` is set it
 *    lazily loads that RG's resources (`useResources`) and renders one page. A resource row raises
 *    `onSelectResource({ armId, sub, rg })` (a {@link TreeSelection}) and marks the selected row.
 *
 * Subscription rows come from the keyset-paginated `GET /_sim/subscriptions` full enumeration
 * (`useSubscriptions`, D-15) — NOT the 500-capped `summary.subscriptions[]` preview (UAT Gap 2). Every
 * level paginates SERVER-SIDE with a BOUNDED, self-contained Prev/Next pager (15-14): it renders ONE
 * page at a time (replace, never append), so the page never grows unbounded. `goNext` parses the
 * `$skiptoken` off the previous `nextLink` with {@link parseSkipToken}, pushes the current cursor onto a
 * `prevTokens` stack, and re-requests the UI's OWN relative builder path — the absolute server-origin
 * `nextLink` is never followed (T-15-17/21). `goPrev` is a pure two-setter transition (WR-03). Each
 * column lives in a height-capped, internally-scrolling region (`.scroll`) so drilling in scrolls
 * rather than lengthening the page (T-15-18 client-DoS mitigation). Deep-link restore: `initialSub`
 * auto-expands that subscription in col1; the URL `rg` (as `selectedRg`) marks the RG row + drives col2.
 *
 * All names/types render as JSX text (auto-escaped) — no `dangerouslySetInnerHTML` (T-15G-05).
 */
import { useEffect, useState } from 'react';

import { useResourceGroups, useResources, useSubscriptions } from '../api/queries';
import { parseSkipToken } from '../api/odata';
import type { ArmResourceGroup, SummarySubscription } from '../api/types';
import styles from './ResourceTree.module.css';

/** The payload raised when a resource row is selected (col2 / search / filter). */
export interface TreeSelection {
  armId: string;
  sub: string;
  rg: string;
}

/** The payload raised when a resource group is selected in col1 (fills col2, clears col3). */
export interface RgSelection {
  sub: string;
  rg: string;
}

/** The short type shown on a resource row: the last segment of the ARM type. */
export function shortType(type: string): string {
  const segments = type.split('/');
  return segments[segments.length - 1] || type;
}

/**
 * The bounded Prev/Next cursor state shared by every level: the current keyset cursor + the stack of
 * prior cursors (for prev). Rendering ONLY the current page keeps the region bounded — pages REPLACE,
 * never accumulate.
 */
function usePager() {
  const [skipToken, setSkipToken] = useState<string | null>(null);
  const [prevTokens, setPrevTokens] = useState<(string | null)[]>([]);

  /** Follow the previous page's `nextLink` (parse its `$skiptoken`, push current, replace). */
  function goNext(nextLink: string | undefined) {
    const token = parseSkipToken(nextLink);
    if (!token) return;
    setPrevTokens((stack) => [...stack, skipToken]);
    setSkipToken(token);
  }

  /** Pure two-setter transition (WR-03): pop the last prior cursor off the stack. */
  function goPrev() {
    if (prevTokens.length === 0) return;
    const prior = prevTokens[prevTokens.length - 1];
    setPrevTokens(prevTokens.slice(0, -1));
    setSkipToken(prior);
  }

  return {
    skipToken,
    hasPrev: prevTokens.length > 0,
    pageIndex: prevTokens.length,
    goNext,
    goPrev,
  };
}

/** A small Prev/Next pager row (buttons + a page readout). REPLACE semantics, never append. */
function Pager({
  label,
  page,
  hasPrev,
  hasNext,
  onPrev,
  onNext,
}: {
  label: string;
  page: number;
  hasPrev: boolean;
  hasNext: boolean;
  onPrev: () => void;
  onNext: () => void;
}) {
  return (
    <div className={styles.pager}>
      <span className={styles.pageReadout}>page {page}</span>
      <button
        type="button"
        className={styles.pageBtn}
        aria-label={`Previous ${label}`}
        onClick={onPrev}
        disabled={!hasPrev}
      >
        ‹ prev
      </button>
      <button
        type="button"
        className={styles.pageBtn}
        aria-label={`Next ${label}`}
        onClick={onNext}
        disabled={!hasNext}
      >
        next ›
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// col1 — SubscriptionColumn (subscriptions + lazy RG list; RG rows SELECT into col2)
// ---------------------------------------------------------------------------

interface SubscriptionColumnProps {
  /** The active subscription scope (from the view's URL params), or null — marks the selected RG. */
  selectedSub: string | null;
  /** The active resource-group scope, or null — marks the selected RG row (gold inset). */
  selectedRg: string | null;
  /** Subscription id to auto-expand on mount (deep-link restore). */
  initialSub?: string | null;
  /** The deep-link RG (accepted for API symmetry with the view; the selection is driven by `selectedRg`). */
  initialRg?: string | null;
  /** Raised when an RG row is clicked — the view lifts it to `?sub&rg` (clearing `?res`) → col2. */
  onSelectRg: (sel: RgSelection) => void;
}

export function SubscriptionColumn({
  selectedSub,
  selectedRg,
  initialSub,
  onSelectRg,
}: SubscriptionColumnProps) {
  // Bounded server-side keyset paging: one page at a time (replace, never accumulate).
  const pager = usePager();
  const { data, isLoading, isError, refetch } = useSubscriptions(
    pager.skipToken ? { skipToken: pager.skipToken } : {},
  );

  const subscriptions = data?.value ?? [];
  const nextLink = data?.nextLink;

  return (
    <div className={styles.column}>
      <div className={styles.colHead}>Subscriptions</div>
      {/* The height-capped scroll region keeps the level list scrolling internally so the page
          never grows unbounded, however deep the tenant. */}
      <div className={styles.scroll} data-testid="subscription-scroll">
        {isLoading && !data ? (
          <SkeletonRows label="Loading subscriptions" />
        ) : isError || !data ? (
          <InlineError text="Could not load subscriptions." onRetry={() => void refetch()} />
        ) : (
          <>
            <ul className={styles.tree} aria-label="Subscriptions">
              {subscriptions.map((sub) => (
                <SubRow
                  key={sub.subscriptionId}
                  sub={sub}
                  defaultOpen={sub.subscriptionId === initialSub}
                  isSelected={sub.subscriptionId === selectedSub}
                  selectedSub={selectedSub}
                  selectedRg={selectedRg}
                  onSelectRg={onSelectRg}
                />
              ))}
            </ul>
            {(pager.hasPrev || nextLink) && (
              <Pager
                label="subscriptions"
                page={pager.pageIndex + 1}
                hasPrev={pager.hasPrev}
                hasNext={Boolean(nextLink)}
                onPrev={pager.goPrev}
                onNext={() => pager.goNext(nextLink)}
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}

interface SubRowProps {
  sub: SummarySubscription;
  defaultOpen: boolean;
  /** This subscription is the active `?sub` scope — marks the row selected (gold inset). */
  isSelected: boolean;
  selectedSub: string | null;
  selectedRg: string | null;
  onSelectRg: (sel: RgSelection) => void;
}

function SubRow({ sub, defaultOpen, isSelected, selectedSub, selectedRg, onSelectRg }: SubRowProps) {
  const [open, setOpen] = useState(defaultOpen);
  // Sync expansion to `initialSub`/`defaultOpen` when it transitions to point at THIS row AFTER
  // mount (WR-01). `useState(defaultOpen)` reads its argument only on the first render, so an
  // in-session search-Subscriptions select (which flips `defaultOpen` true without remounting the
  // row) would update the URL but never expand col1. Expanding on the true-transition also
  // preserves the first-render deep-link behavior (a `?sub` mount still auto-expands) and never
  // force-collapses a row the user opened manually.
  useEffect(() => {
    if (defaultOpen) setOpen(true);
  }, [defaultOpen]);
  return (
    <li className={styles.item}>
      <button
        type="button"
        className={styles.row}
        data-level="subscription"
        data-selected={isSelected}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <Caret open={open} />
        <span className={styles.name}>{sub.name}</span>
        <span className={styles.archetype}>{sub.archetype}</span>
        <span className={styles.count}>{sub.resourceCount}</span>
        {sub.violationCount > 0 && <span className={styles.badge}>{sub.violationCount}</span>}
      </button>
      {open && (
        <RgList
          subId={sub.subscriptionId}
          selectedSub={selectedSub}
          selectedRg={selectedRg}
          onSelectRg={onSelectRg}
        />
      )}
    </li>
  );
}

interface RgListProps {
  subId: string;
  selectedSub: string | null;
  selectedRg: string | null;
  onSelectRg: (sel: RgSelection) => void;
}

function RgList({ subId, selectedSub, selectedRg, onSelectRg }: RgListProps) {
  const pager = usePager();
  const { data, isLoading, isError, refetch } = useResourceGroups(
    subId,
    pager.skipToken ? { skipToken: pager.skipToken } : undefined,
  );

  const groups = data?.value ?? [];
  const nextLink = data?.nextLink;

  if (isLoading && !data) return <SkeletonRows label="Loading resource groups" nested />;
  if (isError) return <InlineError text="Could not load resource groups." onRetry={() => void refetch()} nested />;

  if (groups.length === 0 && !pager.hasPrev) return <EmptyRow text="No resource groups in this subscription." />;

  return (
    <ul className={styles.children}>
      {groups.map((rg) => (
        <RgRow
          key={rg.id}
          subId={subId}
          rg={rg}
          selected={rg.name === selectedRg && subId === selectedSub}
          onSelectRg={onSelectRg}
        />
      ))}
      {(pager.hasPrev || nextLink) && (
        <li className={styles.item}>
          <Pager
            label="resource groups"
            page={pager.pageIndex + 1}
            hasPrev={pager.hasPrev}
            hasNext={Boolean(nextLink)}
            onPrev={pager.goPrev}
            onNext={() => pager.goNext(nextLink)}
          />
        </li>
      )}
    </ul>
  );
}

interface RgRowProps {
  subId: string;
  rg: ArmResourceGroup;
  selected: boolean;
  onSelectRg: (sel: RgSelection) => void;
}

/** An RG row is a SELECT button (EXPL-GAP-02): it drives col2, it does NOT expand a nested list. */
function RgRow({ subId, rg, selected, onSelectRg }: RgRowProps) {
  return (
    <li className={styles.item}>
      <button
        type="button"
        className={styles.resRow}
        data-level="resource-group"
        data-selected={selected}
        onClick={() => onSelectRg({ sub: subId, rg: rg.name })}
      >
        <span className={styles.name}>{rg.name}</span>
      </button>
    </li>
  );
}

// ---------------------------------------------------------------------------
// col2 — ResourceColumn (the selected RG's resources; resource rows SELECT into col3)
// ---------------------------------------------------------------------------

interface ResourceColumnProps {
  /** The selected subscription (from `?sub`), or null. */
  sub: string | null;
  /** The selected resource group (from `?rg`), or null — col2 is a prompt until this is set. */
  rg: string | null;
  /** The currently selected resource ARM id (highlights its row), or null. */
  selectedResId: string | null;
  /**
   * The APPLIED OData `$filter` from the FilterBar (EXPL-05), or undefined. col2 is the SINGLE place
   * the selected RG's resources render, so the filter narrows THIS list (the FilterBar no longer
   * renders its own duplicate list post-Miller).
   */
  filter?: string;
  /** Raised when a resource row is clicked — the view lifts it to `?sub&rg&res` → col3 detail. */
  onSelectResource: (sel: TreeSelection) => void;
}

export function ResourceColumn({ sub, rg, selectedResId, filter, onSelectResource }: ResourceColumnProps) {
  return (
    <div className={styles.column}>
      <div className={styles.colHead}>Resources</div>
      <div className={styles.scroll} data-testid="resource-scroll">
        {sub && rg ? (
          // Keyed by sub/rg/filter so selecting a DIFFERENT RG — or applying/changing the filter —
          // remounts the list, resetting its bounded pager to a fresh cursor (no stale `$skiptoken`
          // from the previous RG or the previous filter's page).
          <ResList
            key={`${sub}/${rg}/${filter ?? ''}`}
            subId={sub}
            rgName={rg}
            filter={filter}
            selectedResId={selectedResId}
            onSelectResource={onSelectResource}
          />
        ) : (
          <p className={styles.prompt}>Select a resource group to list its resources.</p>
        )}
      </div>
    </div>
  );
}

interface ResListProps {
  subId: string;
  rgName: string;
  /** Applied `$filter` (EXPL-05) narrowing this RG's resources, or undefined for the full list. */
  filter?: string;
  selectedResId: string | null;
  onSelectResource: (sel: TreeSelection) => void;
}

function ResList({ subId, rgName, filter, selectedResId, onSelectResource }: ResListProps) {
  const pager = usePager();
  const { data, isLoading, isError, error, refetch } = useResources(subId, rgName, {
    filter,
    skipToken: pager.skipToken ?? undefined,
  });

  const resources = data?.value ?? [];
  const nextLink = data?.nextLink;

  if (isLoading && !data) return <SkeletonRows label="Loading resources" nested />;
  // A bad `$filter` comes back as an ARM 400 — surface the server's fail-closed message (MOCK-06)
  // so the user sees WHY the filter was rejected, not a generic load error.
  if (isError)
    return (
      <InlineError
        text={filter ? `Invalid filter: ${error?.message ?? 'unknown error'}` : 'Could not load resources.'}
        onRetry={() => void refetch()}
        nested
      />
    );

  if (resources.length === 0 && !pager.hasPrev)
    return <EmptyRow text={filter ? 'No resources match this filter.' : 'No resources in this group.'} />;

  return (
    <ul className={styles.tree} aria-label="Resources">
      {resources.map((r) => (
        <li key={r.id} className={styles.item}>
          <button
            type="button"
            className={styles.resRow}
            data-level="resource"
            data-selected={r.id === selectedResId}
            onClick={() => onSelectResource({ armId: r.id, sub: subId, rg: rgName })}
          >
            <span className={styles.name}>{r.name}</span>
            <span className={styles.resType}>{shortType(r.type)}</span>
          </button>
        </li>
      ))}
      {(pager.hasPrev || nextLink) && (
        <li className={styles.item}>
          <Pager
            label="resources"
            page={pager.pageIndex + 1}
            hasPrev={pager.hasPrev}
            hasNext={Boolean(nextLink)}
            onPrev={pager.goPrev}
            onNext={() => pager.goNext(nextLink)}
          />
        </li>
      )}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Shared presentational helpers
// ---------------------------------------------------------------------------

function Caret({ open }: { open: boolean }) {
  return (
    <span className={styles.caret} data-open={open} aria-hidden="true">
      ›
    </span>
  );
}

function SkeletonRows({ label, nested }: { label: string; nested?: boolean }) {
  return (
    <div className={nested ? styles.children : undefined} aria-label={label}>
      <div className={styles.skeleton} />
      <div className={styles.skeleton} />
    </div>
  );
}

function EmptyRow({ text }: { text: string }) {
  return (
    <div className={styles.children}>
      <p className={styles.emptyRow}>{text}</p>
    </div>
  );
}

function InlineError({ text, onRetry, nested }: { text: string; onRetry: () => void; nested?: boolean }) {
  return (
    <div className={nested ? styles.children : undefined}>
      <div className={styles.errorRow}>
        <span className={styles.errorText}>{text}</span>
        <button type="button" className={styles.retry} onClick={onRetry}>
          Retry
        </button>
      </div>
    </div>
  );
}
