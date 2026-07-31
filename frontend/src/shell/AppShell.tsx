/**
 * AppShell — the shared two-pane application shell (WEBUI-04).
 *
 * A `[data-theme]` wrapper (theme is React state, default `dark`) containing:
 *   - a fixed `<aside>` sidebar (brand block + data-driven `SidebarNav` + footer), and
 *   - a main column (`Topbar` + the routed `<Outlet/>` for the current view).
 *
 * These are leaf chrome components with no dependency on the route table: 15-03 (a later wave)
 * mounts `AppShell` as its layout `<Route element>` and wires the section routes into `<Outlet/>`.
 * Rendered here standalone (its own `shell.test.tsx`) so it is testable before routing exists.
 */
import { useState } from 'react';
import { Outlet } from 'react-router';

import ErrorBoundary from './ErrorBoundary';
import SidebarNav from './SidebarNav';
import Topbar from './Topbar';
import styles from './AppShell.module.css';

export type Theme = 'dark' | 'light';

export default function AppShell() {
  const [theme, setTheme] = useState<Theme>('dark');
  const toggleTheme = () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'));

  return (
    <div className={styles.shell} data-theme={theme}>
      <aside className={styles.sidebar}>
        <div className={styles.brand}>
          <div className={styles.brandRow}>
            <span className={styles.brandMark}>◆</span>
            <span className={styles.brandName}>tenantless</span>
          </div>
          <div className={styles.brandSub}>ARM mock console</div>
        </div>

        <SidebarNav />

        <div className={styles.footer}>
          ◆ a Code30 open-source project
          <br />
          Apache-2.0 · self-contained
        </div>
      </aside>

      <div className={styles.main}>
        <Topbar theme={theme} onToggleTheme={toggleTheme} />
        <main className={styles.content}>
          <ErrorBoundary>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
    </div>
  );
}
