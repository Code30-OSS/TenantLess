/**
 * KpiStat — a single header metric (a big Space Mono number + an uppercase micro-label), EXPL-01.
 *
 * Numeric values are thousands-grouped (`102418` → `102,418`) to match the mockup's KPI band; a
 * string value (e.g. the `—` placeholder while the summary is still loading) passes through verbatim.
 */
import styles from './KpiStat.module.css';

interface KpiStatProps {
  value: number | string;
  label: string;
}

export default function KpiStat({ value, label }: KpiStatProps) {
  const display = typeof value === 'number' ? value.toLocaleString('en-US') : value;
  return (
    <div className={styles.stat}>
      <span className={styles.value}>{display}</span>
      <span className={styles.label}>{label}</span>
    </div>
  );
}
