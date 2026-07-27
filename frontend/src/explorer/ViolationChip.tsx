/**
 * ViolationChip + ViolationsBlock — the governance-violation surface (EXPL-03).
 *
 * `ViolationChip` renders `[severity] CODE` in a severity-colored chip (High→--red, Medium→--amber,
 * Low→--green per UI-SPEC). The color token is applied inline from {@link SEVERITY_TOKEN} so the
 * mapping is directly assertable and never depends on a raw hex (D-01).
 *
 * `ViolationsBlock` is the `◆ Governance violations` block hosted by ResourceDetail. It fetches
 * per-resource violations via `useViolations({resource})` and renders one chip per violation, with
 * the loading (`pulse` chip skeletons), empty ("No governance violations." — a clean resource is a
 * valid state, not an error), and error ("Could not load violations." + Retry) states. Its fetch is
 * independent of the detail fetch, so a violations error NEVER blanks the JSON tree beside it.
 *
 * `subscriptionViolationCount` is the small rollup helper the resource tree (15-05) uses to show a
 * per-subscription `violationCount` badge from `summary.subscriptions[]` (the tenant-wide total
 * lives in `totals.violations`; there is no `subscriptionCount` field).
 */
import { useViolations } from '../api/queries';
import type { Severity, Summary } from '../api/types';
import styles from './ViolationChip.module.css';

/** Severity → CSS custom-property token (UI-SPEC EXPL-03 chip colors). */
export const SEVERITY_TOKEN: Record<Severity, string> = {
  High: '--red',
  Medium: '--amber',
  Low: '--green',
};

interface ViolationChipProps {
  severity: Severity;
  code: string;
}

export function ViolationChip({ severity, code }: ViolationChipProps) {
  const token = SEVERITY_TOKEN[severity];
  return (
    <span
      className={styles.chip}
      data-severity={severity}
      style={{ color: `var(${token})`, borderColor: `var(${token})` }}
    >
      <span className={styles.severity}>{severity}</span>
      <span className={styles.code}>{code}</span>
    </span>
  );
}

/** The `◆ Governance violations` block for a single resource (isolated fetch, own error state). */
export function ViolationsBlock({ resource }: { resource: string }) {
  const { data, isLoading, isError, refetch } = useViolations({ resource });

  return (
    <section className={styles.block}>
      <div className={styles.blockTitle}>◆ Governance violations</div>
      {isLoading ? (
        <div className={styles.chipRow} aria-label="Loading violations">
          <span className={styles.chipSkeleton} />
          <span className={styles.chipSkeleton} />
        </div>
      ) : isError ? (
        <div className={styles.errorRow}>
          <span className={styles.errorText}>Could not load violations.</span>
          <button type="button" className={styles.retry} onClick={() => void refetch()}>
            Retry
          </button>
        </div>
      ) : (data?.value.length ?? 0) === 0 ? (
        <p className={styles.emptyText}>No governance violations.</p>
      ) : (
        <div className={styles.chipRow}>
          {data!.value.map((v, i) => (
            <ViolationChip key={`${v.resourceId}:${v.code}:${i}`} severity={v.severity} code={v.code} />
          ))}
        </div>
      )}
    </section>
  );
}

/**
 * Per-subscription violation count from the summary rollup (0 when the subscription is absent).
 * The tree (15-05) renders this as a badge on each subscription row.
 */
export function subscriptionViolationCount(summary: Summary, subscriptionId: string): number {
  const sub = summary.subscriptions.find((s) => s.subscriptionId === subscriptionId);
  return sub?.violationCount ?? 0;
}
