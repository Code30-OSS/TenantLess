/**
 * TenantsManager (CTRL-03 reset + CTRL-04 snapshots, 17-UI-SPEC §4/§5) — the operator's tenant loop.
 *
 * Composes the {@link SnapshotList} (save/restore/delete rows), a safe-name save-name field + `Save
 * snapshot` CTA (→ `useSaveSnapshot`), and a `Reset to empty` action (→ `useReset`). EVERY destructive
 * action (restore / delete / reset) opens the PLAIN {@link ConfirmDialog} with the EXACT UI-SPEC copy
 * before the mutation fires (D-10). All row actions + Save + Reset are disabled while any job runs
 * (single-writer busy lock, D-11); a started job is tracked by the shared {@link JobPanel} and the
 * container's `onStarted`. A missing `pg_dump`/`pg_restore` surfaces as a `failed` job in the panel —
 * never a crash (D-13).
 */
import { useState } from 'react';

import type { ArmError } from '../api/client';
import {
  useDeleteSnapshot,
  useReset,
  useRestoreSnapshot,
  useSaveSnapshot,
  useSnapshots,
} from '../api/control';
import { useSummary } from '../api/queries';
import type { Snapshot } from '../api/types';
import type { ControlSectionProps } from './ControlPlaneView';
import ConfirmDialog from './ConfirmDialog';
import { PrimaryButton, SecondaryButton, TextField } from './fields';
import JobPanel from './JobPanel';
import SnapshotList from './SnapshotList';
import controls from './controls.module.css';
import styles from './tenants.module.css';

/** The pending destructive action awaiting a plain confirm. */
type Pending =
  | { kind: 'restore'; name: string }
  | { kind: 'delete'; name: string }
  | { kind: 'reset' };

const SAFE_NAME = /^[A-Za-z0-9_-]+$/;

/** Safe-name + duplicate validation for the save-name field (T-17-02; server is authoritative). */
function nameError(raw: string, snapshots: Snapshot[]): string | null {
  const name = raw.trim();
  if (name === '') return null; // empty is not an error, just not yet savable (CTA disabled)
  if (!SAFE_NAME.test(name)) return 'Use letters, numbers, dashes or underscores only — no paths.';
  if (snapshots.some((s) => s.name === name))
    return `A snapshot named "${name}" already exists. Choose another name.`;
  return null;
}

/** The exact UI-SPEC confirm copy for each destructive action (Copywriting Contract, locked). */
function confirmCopy(p: Pending): { title: string; body: string; primaryLabel: string } {
  switch (p.kind) {
    case 'restore':
      return {
        title: `Restore "${p.name}"?`,
        body: `This replaces the active tenant with snapshot "${p.name}", including any applied drift.`,
        primaryLabel: 'Restore',
      };
    case 'delete':
      return {
        title: `Delete "${p.name}"?`,
        body: `This permanently deletes the snapshot "${p.name}". This cannot be undone.`,
        primaryLabel: 'Delete',
      };
    case 'reset':
      return {
        title: 'Reset to empty?',
        body: 'This wipes the active tenant. The mock API will serve an empty tenant until you generate or restore one.',
        primaryLabel: 'Reset',
      };
  }
}

export default function TenantsManager({ busy, activeJobId, onStarted }: ControlSectionProps) {
  const snapshotsQuery = useSnapshots();
  const save = useSaveSnapshot();
  const restore = useRestoreSnapshot();
  const del = useDeleteSnapshot();
  const reset = useReset();
  useSummary(); // active-tenant meta is surfaced via the shared topbar; kept warm here (reuse, D-16).

  const [name, setName] = useState('');
  const [touched, setTouched] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [selectedName, setSelectedName] = useState<string | null>(null);

  const snapshots = snapshotsQuery.data ?? [];
  const validationError = nameError(name, snapshots);
  const showNameError = touched ? validationError : null;
  const saveDisabled = busy || save.isPending || name.trim() === '' || validationError !== null;

  const serverError = (save.error ?? restore.error ?? del.error ?? reset.error) as ArmError | null;

  function startSave() {
    if (saveDisabled) return;
    const clean = name.trim();
    save.mutate(
      { name: clean },
      {
        onSuccess: (data) => {
          onStarted(data.job_id);
          setSelectedName(clean);
          setName('');
          setTouched(false);
        },
      },
    );
  }

  function runPending() {
    const p = pending;
    setPending(null);
    if (!p) return;
    if (p.kind === 'restore') {
      restore.mutate(
        { name: p.name },
        {
          onSuccess: (data) => {
            onStarted(data.job_id);
            setSelectedName(p.name);
          },
        },
      );
    } else if (p.kind === 'delete') {
      del.mutate(
        { name: p.name },
        {
          onSuccess: () => {
            if (selectedName === p.name) setSelectedName(null);
          },
        },
      );
    } else {
      reset.mutate(undefined, {
        onSuccess: (data) => {
          onStarted(data.job_id);
          setSelectedName(null);
        },
      });
    }
  }

  return (
    <div className={controls.panel}>
      <div>
        <h1 className={controls.h1}>Manage tenants</h1>
        <p className={controls.subtitle}>
          Save the active tenant as a named snapshot, or restore one — the mock API hot-swaps to it.
        </p>
      </div>

      <div className={styles.saveRow}>
        <TextField
          id="snapshot-name"
          label="SNAPSHOT NAME"
          value={name}
          onChange={setName}
          onBlur={() => setTouched(true)}
          disabled={busy}
          error={showNameError}
        />
        <PrimaryButton onClick={startSave} disabled={saveDisabled}>
          Save snapshot
        </PrimaryButton>
      </div>

      <SnapshotList
        snapshots={snapshots}
        loading={snapshotsQuery.isLoading}
        busy={busy}
        selectedName={selectedName}
        onRestore={(n) => setPending({ kind: 'restore', name: n })}
        onDelete={(n) => setPending({ kind: 'delete', name: n })}
        onSaveFirst={() => setTouched(true)}
      />

      <div className={styles.actions}>
        <SecondaryButton onClick={() => setPending({ kind: 'reset' })} disabled={busy}>
          Reset to empty
        </SecondaryButton>
      </div>

      {serverError && (
        <div className={controls.errorRow} role="alert">
          <span className={controls.errorText}>{serverError.message}</span>
        </div>
      )}

      <JobPanel id={activeJobId} />

      {pending && (
        <ConfirmDialog
          {...confirmCopy(pending)}
          onCancel={() => setPending(null)}
          onConfirm={runPending}
        />
      )}
    </div>
  );
}
