/**
 * AnalyzeForm (CTRL-01, D-12) — fit a statistical profile from an allowlisted DuckDB source.
 *
 * The SOURCE picker is a server-owned allowlist `<select>` (`useSources`) — NEVER a file-path input or
 * upload (T-17-01/D-12). An empty allowlist disables the select with the exact copy and the CTA. The
 * OUTPUT PROFILE NAME is safe-name validated (letters/numbers/dashes/underscores — no paths) with a
 * duplicate-name hint against the existing profile list; the server is the fail-closed authority (it
 * rejects unsafe/existing names before spawning). Submit → `useStartAnalyze` (→ 202 {job_id}); on
 * success the derived profile appears in the Generate PROFILE select (D-12).
 */
import { useState } from 'react';

import type { ArmError } from '../api/client';
import { useProfiles, useSources, useStartAnalyze } from '../api/control';
import type { AnalyzeArgs } from '../api/types';
import type { ControlSectionProps } from './ControlPlaneView';
import { PrimaryButton, SelectField, TextField } from './fields';
import JobPanel from './JobPanel';
import styles from './controls.module.css';

const SAFE_NAME = /^[A-Za-z0-9_-]+$/;

export default function AnalyzeForm({ busy, activeJobId, onStarted }: ControlSectionProps) {
  const sourcesQuery = useSources();
  const profilesQuery = useProfiles();
  const start = useStartAnalyze();

  const [source, setSource] = useState('');
  const [outName, setOutName] = useState('');
  const [touched, setTouched] = useState(false);
  const [lastName, setLastName] = useState('');

  const sources = sourcesQuery.data ?? [];
  const noSources = !sourcesQuery.isLoading && sources.length === 0;
  const existing = new Set((profilesQuery.data ?? []).map((p) => p.name));

  const name = outName.trim();
  let nameError: string | null = null;
  if (name !== '') {
    if (!SAFE_NAME.test(name)) {
      nameError = 'Use letters, numbers, dashes or underscores only — no paths.';
    } else if (existing.has(name)) {
      nameError = `A profile named "${name}" already exists. Choose another name.`;
    }
  }

  const valid = source !== '' && name !== '' && nameError === null && !noSources;
  const serverError = start.error as ArmError | null;

  function submit() {
    if (!valid || busy || start.isPending) return;
    const args: AnalyzeArgs = { source, out_name: name };
    setLastName(name);
    start.mutate(args, { onSuccess: (data) => onStarted(data.job_id) });
  }

  const ctaLabel = busy ? 'Busy — a job is running' : start.isPending ? 'Starting…' : 'Run analyze';

  return (
    <div className={styles.panel}>
      <div>
        <h1 className={styles.h1}>Analyze a data source</h1>
        <p className={styles.subtitle}>
          Fit a statistical profile from an allowlisted DuckDB source. Live-Azure scan is not
          available here.
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
          id="analyze-source"
          label="SOURCE"
          value={source}
          onChange={setSource}
          disabled={busy || noSources || sourcesQuery.isLoading}
          placeholder={sourcesQuery.isLoading ? 'Loading sources…' : 'Select a source'}
          options={sources.map((s) => ({ value: s.name, label: s.name }))}
          hint={noSources ? 'No allowlisted sources on this server.' : undefined}
        />

        <TextField
          id="analyze-out"
          label="OUTPUT PROFILE NAME"
          value={outName}
          onChange={setOutName}
          onBlur={() => setTouched(true)}
          placeholder="my-derived-profile"
          disabled={busy || noSources}
          error={touched ? nameError : null}
        />

        {serverError && (
          <div className={styles.errorRow} role="alert">
            <span className={styles.errorText}>{serverError.message}</span>
          </div>
        )}

        {start.isSuccess && (
          <p className={styles.successNote}>Profile "{lastName}" is now available in Generate.</p>
        )}

        <PrimaryButton type="submit" disabled={!valid || busy || start.isPending}>
          {ctaLabel}
        </PrimaryButton>
      </form>

      <JobPanel id={activeJobId} />
    </div>
  );
}
