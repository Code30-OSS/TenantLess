/**
 * CountBars — the CONS-02 zero-dependency, hand-rolled SVG horizontal bar chart (D-05). Renders a
 * by-route or by-status count breakdown as one `<rect>` per category, the bar width driven by the pure
 * `scale.ts` `linScale(0, maxCount, 0, plotWidth)` (the largest count fills the plot). Sorted count-desc
 * with a muted `+N more` tail; token-only colors throughout (no raw hex).
 *
 * Color discipline: status bars read `STATUS_TOKEN` (from `api/console.ts`, the sole owner of the map)
 * keyed by `statusClass` (from `console/filter.ts`, the sole owner of the band classifier); route bars
 * use `--text-2`. The selected bar switches to `--gold` fill + a gold left-accent — the CONS-02
 * cross-highlight signal. Clicking a bar raises `onSelect(key)`; clicking the already-selected bar
 * raises `onSelect(null)` (toggle-clear).
 *
 * Every category label / count reaches the DOM as auto-escaped JSX text (T-16-03: a scanner-injected
 * `route` cannot inject markup; no `dangerouslySetInnerHTML`, colors are `var(--token)` strings).
 */
import { STATUS_TOKEN } from '../api/console';
import { statusClass, type StatusClass } from './filter';
import { linScale } from './scale';
import styles from './CountBars.module.css';

/** SVG plot width in viewBox units — the bar `<svg>` scales to 100% via `preserveAspectRatio`. */
const PLOT_W = 100;
/** Bar row height in viewBox units. */
const BAR_H = 12;
/** Cap the rendered rows; the remainder collapses into a muted `+N more` tail. */
const MAX_ROWS = 8;

interface CountBarsProps {
  /** `route` bars use `--text-2`; `status` bars use the STATUS_TOKEN color for their class. */
  variant: 'route' | 'status';
  /** Category → count (server session counters or client-derived filtered counts). */
  counts: Record<string, number>;
  /** The currently selected category (drill-down), or `null` when nothing is selected. */
  selected: string | null;
  /** Toggle a selection; the caller passes `null` to clear. */
  onSelect: (key: string | null) => void;
  /** Block sub-label rendered verbatim (`by route · session` vs `by route · last 5m`). */
  label: string;
}

/** Resolve a status count key (`"200"` code or `"2xx"` class) to its STATUS_TOKEN color name. */
function statusToken(key: string): string {
  const cls: StatusClass = /^\d+$/.test(key) ? statusClass(Number(key)) : (key as StatusClass);
  return STATUS_TOKEN[cls] ?? '--text-2';
}

export default function CountBars({ variant, counts, selected, onSelect, label }: CountBarsProps) {
  const title = variant === 'route' ? '◆ By route' : '◆ By status';
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);

  const body = (() => {
    if (entries.length === 0) {
      // Selection present but nothing survives → filter excluded everything (offer a Clear).
      if (selected !== null) {
        return (
          <div className={styles.errorRow}>
            <span className={styles.emptyText}>No requests match this filter.</span>
            <button type="button" className={styles.clear} onClick={() => onSelect(null)}>
              Clear filter
            </button>
          </div>
        );
      }
      return <p className={styles.emptyText}>No requests recorded yet.</p>;
    }

    const maxCount = Math.max(...entries.map(([, c]) => c), 1);
    const xScale = linScale(0, maxCount, 0, PLOT_W);
    const visible = entries.slice(0, MAX_ROWS);
    const hidden = entries.length - visible.length;

    return (
      <div className={styles.rows}>
        {visible.map(([key, count]) => {
          const isSelected = key === selected;
          const fill = isSelected
            ? '--gold'
            : variant === 'status'
              ? statusToken(key)
              : '--text-2';
          return (
            <button
              type="button"
              key={key}
              data-key={key}
              data-selected={isSelected || undefined}
              className={styles.row}
              onClick={() => onSelect(isSelected ? null : key)}
            >
              <span className={styles.label} title={key}>
                {key}
              </span>
              <svg
                className={styles.bar}
                viewBox={`0 0 ${PLOT_W} ${BAR_H}`}
                width="100%"
                height={BAR_H}
                preserveAspectRatio="none"
                aria-hidden="true"
              >
                <rect
                  x={0}
                  y={0}
                  width={xScale(count)}
                  height={BAR_H}
                  fill={`var(${fill})`}
                  data-key={key}
                />
              </svg>
              <span className={styles.count}>{count.toLocaleString('en-US')}</span>
            </button>
          );
        })}
        {hidden > 0 && <div className={styles.more}>+{hidden} more</div>}
      </div>
    );
  })();

  return (
    <section className={styles.block}>
      <div className={styles.title}>{title}</div>
      <div className={styles.sublabel}>{label}</div>
      {body}
    </section>
  );
}
