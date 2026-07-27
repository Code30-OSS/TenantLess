/**
 * GenerateForm (CTRL-01, D-08/D-03) — the operator's generate screen.
 *
 * Fields map 1:1 to the `generate` CLI flags (17-UI-SPEC §1): PROFILE (`useProfiles` allowlist, D-12),
 * SEED (i64), SUBSCRIPTIONS (≤ 5000), TARGET RESOURCES (≤ 500000), --JOBS PARALLELISM (≤ cores), the
 * GOVERNANCE-VIOLATION INJECTION RATE slider (0–100%, default 12%), and the over-privilege toggle.
 *
 * The client caps + copy are UX affordances — the server re-validates and is authoritative (T-17-01).
 * Per D-08 the slider maps to `--violations` ON/OFF (value > 0 ⇒ on), NOT a granular server-side rate.
 * Submit → `useStartGenerate` (→ 202 {job_id}); the container tracks the job and renders busy state.
 */
import { useState } from 'react';

import type { ArmError } from '../api/client';
import { useProfiles, useStartGenerate } from '../api/control';
import { useSummary } from '../api/queries';
import type { GenerateArgs } from '../api/types';
import type { ControlSectionProps } from './ControlPlaneView';
import ConfirmDialog from './ConfirmDialog';
import {
  NumberField,
  PrimaryButton,
  SelectField,
  ToggleSwitch,
  ViolationRateSlider,
} from './fields';
import JobPanel from './JobPanel';
import styles from './controls.module.css';

/** Logical cores drive the --jobs cap (the browser's honest source; the server clamps to [1, cores]). */
const CORES = typeof navigator !== 'undefined' ? navigator.hardwareConcurrency || 4 : 4;

const RES_MAX = 500000;
const SUBS_MAX = 5000;

/** Parse a strict base-10 integer (rejects blanks, decimals, and non-numerics). */
function parseInteger(raw: string): number | null {
  const s = raw.trim();
  if (!/^-?\d+$/.test(s)) return null;
  return Number(s);
}

function seedError(raw: string): string | null {
  return parseInteger(raw) === null ? 'Seed must be a whole number (64-bit).' : null;
}
function subsError(raw: string): string | null {
  const n = parseInteger(raw);
  return n === null || n < 1 || n > SUBS_MAX ? 'Subscriptions must be between 1 and 5,000.' : null;
}
function resourcesError(raw: string): string | null {
  const n = parseInteger(raw);
  return n === null || n < 1 || n > RES_MAX
    ? 'Target resources must be between 1 and 500,000.'
    : null;
}
function jobsError(raw: string): string | null {
  const n = parseInteger(raw);
  return n === null || n < 1 || n > CORES
    ? `Parallelism must be between 1 and ${CORES} (available cores).`
    : null;
}

export default function GenerateForm({ busy, activeJobId, onStarted }: ControlSectionProps) {
  const profilesQuery = useProfiles();
  const start = useStartGenerate();
  const summary = useSummary();

  const [profile, setProfile] = useState('');
  const [seed, setSeed] = useState('42');
  const [subscriptions, setSubscriptions] = useState('');
  const [resources, setResources] = useState('');
  const [jobs, setJobs] = useState('1');
  const [violationRate, setViolationRate] = useState(12);
  const [overPrivilege, setOverPrivilege] = useState(true);
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  const [confirmOpen, setConfirmOpen] = useState(false);

  // Generate is truncate-and-replace: when a tenant is ALREADY active it is destructive, so the
  // submit first opens the plain regenerate confirm (D-10). On an EMPTY tenant there is nothing to
  // replace, so submit fires directly. `useSummary` reports the active tenant (tenantId null ⇒ empty).
  const hasActiveTenant = Boolean(summary.data?.tenantId);

  const errors = {
    seed: seedError(seed),
    subscriptions: subsError(subscriptions),
    resources: resourcesError(resources),
    jobs: jobsError(jobs),
  };
  const profileMissing = profile.trim() === '';
  const valid = !profileMissing && Object.values(errors).every((e) => e === null);

  const profiles = profilesQuery.data ?? [];
  const profilesLoading = profilesQuery.isLoading;
  const serverError = start.error as ArmError | null;

  function touch(field: string) {
    setTouched((t) => ({ ...t, [field]: true }));
  }
  function show(field: keyof typeof errors): string | null {
    return touched[field] ? errors[field] : null;
  }

  function submit() {
    if (!valid || busy || start.isPending) return;
    // Regenerating over an active tenant is destructive → gate behind the plain confirm (D-10).
    if (hasActiveTenant) {
      setConfirmOpen(true);
      return;
    }
    doStart();
  }

  function doStart() {
    const args: GenerateArgs = {
      profile,
      seed: Number(parseInteger(seed)),
      subscriptions: Number(parseInteger(subscriptions)),
      resources: Number(parseInteger(resources)),
      jobs: Number(parseInteger(jobs)),
      violations: violationRate > 0,
      over_privilege: overPrivilege,
    };
    start.mutate(args, { onSuccess: (data) => onStarted(data.job_id) });
  }

  const ctaLabel = busy
    ? 'Busy — a job is running'
    : start.isPending
      ? 'Starting…'
      : 'Generate tenant';

  return (
    <div className={styles.panel}>
      <div>
        <h1 className={styles.h1}>Generate synthetic tenant</h1>
        <p className={styles.subtitle}>
          Byte-reproducible from a seed: subscriptions → resource groups → ARM-valid resources.
        </p>
      </div>

      <form
        className={styles.form}
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <SelectField
          id="gen-profile"
          label="PROFILE"
          value={profile}
          onChange={setProfile}
          disabled={busy || profilesLoading}
          placeholder={profilesLoading ? 'Loading profiles…' : 'Select a profile'}
          options={profiles.map((p) => ({ value: p.name, label: p.name }))}
        />

        <div className={styles.formGrid}>
          <NumberField
            id="gen-seed"
            label="SEED"
            value={seed}
            onChange={setSeed}
            onBlur={() => touch('seed')}
            disabled={busy}
            error={show('seed')}
          />
          <NumberField
            id="gen-jobs"
            label="--JOBS PARALLELISM"
            value={jobs}
            onChange={setJobs}
            onBlur={() => touch('jobs')}
            min={1}
            max={CORES}
            disabled={busy}
            error={show('jobs')}
          />
          <NumberField
            id="gen-subscriptions"
            label="SUBSCRIPTIONS"
            value={subscriptions}
            onChange={setSubscriptions}
            onBlur={() => touch('subscriptions')}
            min={1}
            max={SUBS_MAX}
            disabled={busy}
            error={show('subscriptions')}
          />
          <NumberField
            id="gen-resources"
            label="TARGET RESOURCES"
            value={resources}
            onChange={setResources}
            onBlur={() => touch('resources')}
            min={1}
            max={RES_MAX}
            disabled={busy}
            error={show('resources')}
          />
        </div>

        <ViolationRateSlider
          id="gen-violations"
          label="GOVERNANCE-VIOLATION INJECTION RATE"
          value={violationRate}
          onChange={setViolationRate}
          disabled={busy}
          hint={
            violationRate > 0
              ? 'Violations on — per-code rates are profile-driven.'
              : 'Violations off.'
          }
        />

        <ToggleSwitch
          id="gen-over-privilege"
          label="OVER-PRIVILEGE INJECTION"
          checked={overPrivilege}
          onChange={setOverPrivilege}
          disabled={busy}
        />

        {serverError && (
          <div className={styles.errorRow} role="alert">
            <span className={styles.errorText}>{serverError.message}</span>
          </div>
        )}

        <div className={styles.tokenRow}>
          <PrimaryButton type="submit" disabled={!valid || busy || start.isPending}>
            {ctaLabel}
          </PrimaryButton>
          <span className={styles.statusLine}>
            {busy ? 'busy · a job is running' : `idle · ready · ${CORES} workers`}
          </span>
        </div>
      </form>

      <JobPanel id={activeJobId} />

      {confirmOpen && (
        <ConfirmDialog
          title="Regenerate tenant?"
          body="This replaces the active tenant currently served by the mock API."
          primaryLabel="Regenerate"
          onCancel={() => setConfirmOpen(false)}
          onConfirm={() => {
            setConfirmOpen(false);
            doStart();
          }}
        />
      )}
    </div>
  );
}
