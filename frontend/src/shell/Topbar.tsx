/**
 * Topbar — the shell header (WEBUI-04).
 *
 * Left: a breadcrumb region (populated by views later) + the live tenant-meta line.
 * Right: the ThemeToggle, a status pill whose dot/label follow the summary query state, a manual
 * Refresh command, and the gold mock-server URL.
 *
 * Live metadata (UAT Gap 4): seed / profile / tenantId come from `useSummary` — the SHARED
 * `['summary']` cache (no second fetch) — with muted em-dash placeholders while loading/absent so an
 * undefined summary never crashes and never fabricates a value. The status pill is derived from the
 * query state (loading → connecting, error → error, else running), and the URL renders
 * `window.location.origin` (D-06 same-origin embed under /ui), not a hardcoded host.
 *
 * Manual Refresh (UAT Gap 5, D-04): the app is no-poll (staleTime Infinity + implicit refetch off in
 * main.tsx). Refresh is the ONLY re-fetch path — it calls `queryClient.invalidateQueries()` on click;
 * no polling / refetchInterval is reintroduced here.
 *
 * Summary string values render as auto-escaped JSX text (no dangerouslySetInnerHTML) — T-15-13.
 */
import { useQueryClient } from '@tanstack/react-query';

import { useSummary } from '../api/queries';
import type { Theme } from './AppShell';
import ThemeToggle from './ThemeToggle';
import styles from './Topbar.module.css';

interface TopbarProps {
  theme: Theme;
  onToggleTheme: () => void;
}

/** Truncate a tenant GUID to the mockup's `8f3c1a90…4f31` shape without fabricating characters. */
function shortTenant(id: string): string {
  return id.length > 13 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
}

export default function Topbar({ theme, onToggleTheme }: TopbarProps) {
  const { data, isLoading, isError } = useSummary();
  const queryClient = useQueryClient();

  const seedText = data ? String(data.seed) : '—';
  const profileText = data?.profile ?? '—';
  const tenantText = data ? shortTenant(data.tenantId) : '—';

  const status = isLoading ? 'connecting' : isError ? 'error' : 'running';

  return (
    <header className={styles.topbar}>
      <div className={styles.left}>
        {/* Breadcrumb region — filled by the Explorer drill-down in a later plan. */}
        <div className={styles.crumb} aria-label="Breadcrumb" />
        <div className={styles.divider} aria-hidden="true" />
        <div className={styles.meta}>
          seed <span className={styles.metaHi}>{seedText}</span> · profile{' '}
          <span className={styles.metaVal}>{profileText}</span> · tenant_id{' '}
          <span className={styles.metaFaint}>{tenantText}</span>
        </div>
      </div>

      <div className={styles.right}>
        <ThemeToggle theme={theme} onToggle={onToggleTheme} />
        <button
          type="button"
          aria-label="Refresh"
          className={styles.themeToggle}
          onClick={() => queryClient.invalidateQueries()}
        >
          <span className={styles.themeSeg}>refresh</span>
        </button>
        <div className={styles.pill} data-status={status}>
          <span className={styles.dot} aria-hidden="true" />
          <span className={styles.running}>{status}</span>
        </div>
        <div className={styles.mock}>
          mock <span className={styles.url}>{window.location.origin}</span>
        </div>
      </div>
    </header>
  );
}
