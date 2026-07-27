/**
 * ServerStatusView (D-09/D-16, 17-UI-SPEC §6) — a light, REUSE-ONLY server-status screen.
 *
 * It renders ONLY data already available to the app: `useSummary` (`/_sim/summary` — the SAME feed the
 * topbar reads) plus `window.location` for the mock URL. There is NO new backend status endpoint
 * (D-16, Assumption #5): any datum not in the existing responses is derived from `window.location` or
 * omitted. On an empty tenant (post-reset, `tenantId` null) it shows "No active tenant" + zero counts
 * rather than crashing (D-09). Counts reuse the Explorer `KpiStat`.
 */
import { useSummary } from '../api/queries';
import KpiStat from '../explorer/KpiStat';
import controls from './controls.module.css';
import styles from './tenants.module.css';

export default function ServerStatusView() {
  const { data: summary, isLoading, isError } = useSummary();

  const totals = summary?.totals;
  const active = Boolean(summary?.tenantId);
  const origin = typeof window !== 'undefined' ? window.location.origin : '';

  return (
    <div className={controls.panel}>
      <div>
        <h1 className={controls.h1}>Server status</h1>
        <p className={controls.subtitle}>
          The live mock API — reusing the tenant summary the topbar already serves (no new endpoint).
        </p>
      </div>

      {isLoading ? (
        <div className={styles.statusHead}>
          <span className={`${controls.pill} ${styles.pillIdle}`}>connecting</span>
        </div>
      ) : (
        <>
          <div className={styles.statusHead}>
            <span className={`${controls.pill} ${active ? styles.pillRunning : styles.pillIdle}`}>
              {active ? 'running' : 'No active tenant'}
            </span>
            {active && (
              <span className={styles.metaLine}>
                seed {summary!.seed} · profile {summary!.profile} · {summary!.tenantId}
              </span>
            )}
          </div>

          <div className={styles.kpiRow}>
            <KpiStat value={totals?.subscriptions ?? 0} label="SUBSCRIPTIONS" />
            <KpiStat value={totals?.resourceGroups ?? 0} label="RESOURCE GROUPS" />
            <KpiStat value={totals?.resources ?? 0} label="RESOURCES" />
            <KpiStat value={totals?.violations ?? 0} label="VIOLATIONS" />
          </div>

          {active ? (
            <span className={styles.mockUrl}>mock {origin}</span>
          ) : (
            <p className={controls.subtitle}>
              Generate a tenant or restore a snapshot to start serving ARM.
            </p>
          )}

          {isError && <p className={controls.statusLine}>Status unavailable — the server is unreachable.</p>}
        </>
      )}
    </div>
  );
}
