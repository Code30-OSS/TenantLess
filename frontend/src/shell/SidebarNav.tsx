/**
 * SidebarNav — the data-driven grouped navigation (WEBUI-04).
 *
 * Iterates `NAV_MODEL` (nav.ts) → groups → items, rendering each item as a react-router `NavLink`.
 * The active item (matching the current location) carries the gold left-accent + `--text` label;
 * idle items are `--text-2`. Active state is derived by `NavLink` (which also sets
 * `aria-current="page"` on the active link — the accessible + testable active signal).
 *
 * Adding a section later (e.g. Identity/RBAC) is an append to `NAV_MODEL`; this component needs no
 * edit.
 */
import { NavLink } from 'react-router';

import { NAV_MODEL } from './nav';
import styles from './SidebarNav.module.css';

export default function SidebarNav() {
  return (
    <nav className={styles.nav} aria-label="Primary">
      {NAV_MODEL.map((group) => (
        <div key={group.title} className={styles.group} data-nav-group={group.title}>
          <div className={styles.groupTitle}>{group.title}</div>
          {group.items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                isActive ? `${styles.item} ${styles.itemActive}` : styles.item
              }
            >
              <span className={styles.dot} aria-hidden="true" />
              <span className={styles.label}>{item.label}</span>
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  );
}
