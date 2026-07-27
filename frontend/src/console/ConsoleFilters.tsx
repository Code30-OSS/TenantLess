/**
 * ConsoleFilters (CONS-04) — the Observability Console's INSTANT-APPLY filter bar. A stateless,
 * fully-controlled component: it owns NO internal pending/committed state (unlike the Explorer's
 * `FilterBar`, whose Enter/blur-commit split existed only to avoid per-keystroke SERVER round-trips —
 * there is no server round-trip here, the filter is 100% client-side over the in-memory event buffer,
 * so every chip toggles `onChange` IMMEDIATELY, RESEARCH Pattern 7).
 *
 * Three chip rows over the single shared `{ status, route, window }` filter:
 *  - **status** — `2xx/3xx/4xx/5xx`, MULTI-select toggle; an active chip carries its
 *    `var(${STATUS_TOKEN[class]})` status color (mirrors the `ViolationChip.SEVERITY_TOKEN` idiom).
 *  - **route** — the observed `route` templates (from `stats.by_route` keys), SINGLE-select; the
 *    active chip is gold-outlined (`var(--gold)`), matching the shipped `.next`/`.resRow[data-selected]`.
 *  - **window** — fixed presets `1m/5m/15m/All` (D-08), SINGLE-select, default `5m`, gold active.
 * A `clear filters` affordance resets to `{ status: [], route: null, window: '5m' }`.
 *
 * Color discipline: chips reference `var(--token)` ONLY (never a raw hex — directly assertable); the
 * scanner-controlled `route` strings reach the DOM only as auto-escaped JSX text (T-16-03).
 */
import { STATUS_TOKEN } from '../api/console';
import type { ConsoleFilter, StatusClass, WindowPreset } from './filter';
import styles from './ConsoleFilters.module.css';

/** The status-class domain, left→right (CONS-04). */
const STATUS_CLASSES: readonly StatusClass[] = ['2xx', '3xx', '4xx', '5xx'];

/** The fixed window presets (D-08) — value is the `WindowPreset` key, label the chip text. */
const WINDOWS: ReadonlyArray<{ value: WindowPreset; label: string }> = [
  { value: '1m', label: '1m' },
  { value: '5m', label: '5m' },
  { value: '15m', label: '15m' },
  { value: 'all', label: 'All' },
];

/** The default filter — the `clear filters` reset target (window defaults to `5m`, D-08). */
export const DEFAULT_FILTER: ConsoleFilter = { status: new Set(), route: null, window: '5m' };

interface ConsoleFiltersProps {
  /** The single shared filter (owned by `ConsoleView`); this component is fully controlled. */
  filter: ConsoleFilter;
  /** The observed route templates (from `stats.by_route` keys). */
  routes: string[];
  /** Raised with the next filter on EVERY interaction — instant-apply, no pending buffer. */
  onChange: (next: ConsoleFilter) => void;
}

export default function ConsoleFilters({ filter, routes, onChange }: ConsoleFiltersProps) {
  /** Toggle a status class in/out of the multi-select set (returns a fresh Set — never mutates). */
  function toggleStatus(cls: StatusClass) {
    const next = new Set(filter.status);
    if (next.has(cls)) next.delete(cls);
    else next.add(cls);
    onChange({ ...filter, status: next });
  }

  /** Single-select a route; clicking the active route clears it (toggle to `null`). */
  function selectRoute(route: string) {
    onChange({ ...filter, route: filter.route === route ? null : route });
  }

  /** Single-select a window preset. */
  function selectWindow(window: WindowPreset) {
    onChange({ ...filter, window });
  }

  function clear() {
    onChange({ status: new Set(), route: null, window: '5m' });
  }

  const hasFilter = filter.status.size > 0 || filter.route !== null || filter.window !== '5m';

  return (
    <div className={styles.bar}>
      <div className={styles.row}>
        <span className={styles.label}>status</span>
        <div className={styles.chips}>
          {STATUS_CLASSES.map((cls) => {
            const active = filter.status.has(cls);
            return (
              <button
                key={cls}
                type="button"
                data-status={cls}
                data-active={active || undefined}
                className={styles.chip}
                // Active status chip carries its status token color (assertable, no raw hex).
                style={
                  active
                    ? { color: `var(${STATUS_TOKEN[cls]})`, borderColor: `var(${STATUS_TOKEN[cls]})` }
                    : undefined
                }
                aria-pressed={active}
                onClick={() => toggleStatus(cls)}
              >
                {cls}
              </button>
            );
          })}
        </div>
      </div>

      {routes.length > 0 && (
        <div className={styles.row}>
          <span className={styles.label}>route</span>
          <div className={styles.chips}>
            {routes.map((route) => {
              const active = filter.route === route;
              return (
                <button
                  key={route}
                  type="button"
                  data-route={route}
                  data-active={active || undefined}
                  className={`${styles.chip} ${active ? styles.chipGold : ''}`}
                  aria-pressed={active}
                  title={route}
                  onClick={() => selectRoute(route)}
                >
                  {route}
                </button>
              );
            })}
          </div>
        </div>
      )}

      <div className={styles.row}>
        <span className={styles.label}>window</span>
        <div className={styles.chips}>
          {WINDOWS.map(({ value, label }) => {
            const active = filter.window === value;
            return (
              <button
                key={value}
                type="button"
                data-window={value}
                data-active={active || undefined}
                className={`${styles.chip} ${active ? styles.chipGold : ''}`}
                aria-pressed={active}
                onClick={() => selectWindow(value)}
              >
                {label}
              </button>
            );
          })}
        </div>

        {hasFilter && (
          <button type="button" className={styles.clear} onClick={clear}>
            clear filters
          </button>
        )}
      </div>
    </div>
  );
}
