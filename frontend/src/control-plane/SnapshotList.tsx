/**
 * SnapshotList (CTRL-04, 17-UI-SPEC §4) — the named-snapshot rows for the TenantsManager.
 *
 * Rows mirror the FilterBar `.resRow` selectable idiom (name · created-at) with a per-row
 * `restore` + `delete`; the row matching the active tenant carries the gold `inset 2px 0 0 var(--gold)`
 * selected rule. States: loading → skeleton rows; empty → an empty-state card ("No snapshots yet" +
 * a `Save current tenant as snapshot` CTA); populated → the rows. All row actions are disabled while
 * any job runs (busy lock, D-11) — the actual confirm gating + mutation live in the TenantsManager.
 */
import type { Snapshot } from '../api/types';
import { PrimaryButton } from './fields';
import styles from './tenants.module.css';

export interface SnapshotListProps {
  snapshots: Snapshot[];
  loading: boolean;
  busy: boolean;
  /** The currently-active snapshot name (last saved/restored), highlighted with the gold rule. */
  selectedName: string | null;
  onRestore: (name: string) => void;
  onDelete: (name: string) => void;
  /** The empty-state CTA (prompts a save of the current tenant). */
  onSaveFirst: () => void;
}

/** The created-at meta for a row: the server's `createdUnix` (Unix seconds) as a readable date. */
function rowMeta(s: Snapshot): string {
  return new Date(s.createdUnix * 1000).toLocaleString();
}

export default function SnapshotList({
  snapshots,
  loading,
  busy,
  selectedName,
  onRestore,
  onDelete,
  onSaveFirst,
}: SnapshotListProps) {
  if (loading) {
    return (
      <div aria-label="Loading snapshots">
        <div className={styles.skeleton} />
        <div className={styles.skeleton} />
      </div>
    );
  }

  if (snapshots.length === 0) {
    return (
      <div className={styles.emptyCard}>
        <p className={styles.emptyTitle}>No snapshots yet</p>
        <p className={styles.emptyBody}>Save the active tenant as a snapshot to restore it later.</p>
        <PrimaryButton disabled={busy} onClick={onSaveFirst}>
          Save current tenant as snapshot
        </PrimaryButton>
      </div>
    );
  }

  return (
    <ul className={styles.list}>
      {snapshots.map((s) => {
        const selected = s.name === selectedName;
        return (
          <li key={s.name}>
            <div
              className={selected ? `${styles.row} ${styles.rowSelected}` : styles.row}
              data-selected={selected}
            >
              <div className={styles.rowMain}>
                <span className={styles.rowName}>{s.name}</span>
                <span className={styles.rowMeta}>{rowMeta(s)}</span>
              </div>
              <div className={styles.rowActions}>
                <button
                  type="button"
                  className={styles.rowBtn}
                  disabled={busy}
                  onClick={() => onRestore(s.name)}
                >
                  restore
                </button>
                <button
                  type="button"
                  className={styles.rowBtnDanger}
                  disabled={busy}
                  onClick={() => onDelete(s.name)}
                >
                  delete
                </button>
              </div>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
