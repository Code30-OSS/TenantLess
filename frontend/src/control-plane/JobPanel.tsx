/**
 * JobPanel (CTRL-02, D-15) — THE live job tracker for the control plane.
 *
 * Consumes `useJob(id)` (17-03), which polls `GET /_control/jobs/{id}` ONLY while the job is
 * `queued`/`running` and STOPS on a terminal status (`jobRefetchInterval`, D-07). This component is
 * purely presentational over that snapshot: a status pill (amber queued/running, green succeeded, red
 * failed), the coarse phase label, an elapsed timer (decorative, `aria-hidden`), and a bounded
 * monospace log tail.
 *
 * Accessibility (17-UI-SPEC §Accessibility): the status+phase+result region is
 * `role="status" aria-live="polite"` so `queued→running→succeeded/failed` and phase-label changes are
 * announced — but the scrolling log tail is a NON-live `<pre aria-label="Job log">` (a live region
 * would flood a screen reader with every log line).
 */
import { useEffect, useRef, useState } from 'react';

import { useJob } from '../api/control';
import type { JobResult, JobStatus } from '../api/types';
import styles from './controls.module.css';

/** The bounded log-tail size (D-06): render only the last N lines, wrap long tokens `break-all`. */
export const LOG_TAIL_MAX = 200;

const PILL_CLASS: Record<JobStatus, string> = {
  queued: styles.pillQueued,
  running: styles.pillRunning,
  succeeded: styles.pillSucceeded,
  failed: styles.pillFailed,
};

function JobStatusPill({ status }: { status: JobStatus }) {
  return <span className={`${styles.pill} ${PILL_CLASS[status]}`}>{status}</span>;
}

/** The parsed success summary (D-08) or the exit-0-but-unparsed fallback. */
function successLine(result: JobResult | undefined): string {
  if (!result) return 'Completed — exit 0. See the log for details.';
  const short = result.tenant_id ? result.tenant_id.slice(0, 8) : '—';
  const parts = [
    `${result.subscriptions ?? 0} subscriptions`,
    `${result.resource_groups ?? 0} resource groups`,
    `${result.resources ?? 0} resources`,
    `${result.violations ?? 0} violations`,
  ];
  return `Succeeded — tenant ${short}: ${parts.join(' · ')}.`;
}

/**
 * A decorative, whole-second elapsed timer that ticks only while the job is non-terminal.
 *
 * WR-02: the start reference is anchored to the JOB (keyed by `id`), not to component mount — the
 * panel is mounted persistently for the section, so a mount-anchored ref counts the time the
 * operator sat on the screen BEFORE starting the job (and drifts further across a second job).
 * Re-anchoring on every `id` change (idle→job, or job→next job) measures each job from its own
 * start. The server's `Job` has a `started_at` but it is intentionally not on the wire (JobSnapshot
 * omits the Instant), so the client anchors to the job's first-seen instant.
 */
function useElapsedSeconds(active: boolean, id: string | null): number {
  const start = useRef<number>(Date.now());
  const [seconds, setSeconds] = useState(0);
  // Re-anchor when the tracked job changes (a fresh id ⇒ a fresh job start).
  useEffect(() => {
    start.current = Date.now();
    setSeconds(0);
  }, [id]);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setSeconds(Math.floor((Date.now() - start.current) / 1000)), 1000);
    return () => clearInterval(t);
  }, [active]);
  return seconds;
}

export default function JobPanel({ id }: { id: string | null }) {
  const { data: job } = useJob(id);
  const active = job?.status === 'queued' || job?.status === 'running';
  const seconds = useElapsedSeconds(Boolean(active), id);

  // none state: no active job → the panel is absent (screens show their idle status line instead).
  if (!id || !job) return null;

  const tail = job.log.slice(-LOG_TAIL_MAX);

  return (
    <div className={styles.jobPanel}>
      <div className={styles.jobHead} role="status" aria-live="polite">
        <JobStatusPill status={job.status} />
        {job.phase && <span className={styles.phase}>{job.phase}</span>}
        {job.status === 'succeeded' && (
          <span className={styles.resultLine}>{successLine(job.result)}</span>
        )}
        <span className={styles.elapsed} aria-hidden="true">
          {seconds}s
        </span>
      </div>

      <pre className={styles.log} aria-label="Job log">
        {tail.join('\n')}
      </pre>

      {job.status === 'failed' && (
        <div className={styles.recovery} role="alert">
          Job failed. The tenant may be left in a partial state — reset to empty or regenerate to
          recover.
        </div>
      )}
    </div>
  );
}
