/**
 * JobContext — the app-level owner of the single active control-plane job (lifecycle half).
 *
 * Mounted ABOVE `<Routes>` in App.tsx, the `JobProvider` NEVER unmounts on route change. It holds the
 * one `activeJobId`, runs the active-only `useJob` poll and the completion-driven
 * `useInvalidateOnJobSuccess` watcher internally, and exposes `{ activeJobId, activeJob, busy, reportJob }`
 * via context. Because the poll + terminal invalidation live here (not in `ControlPlaneView`), a job that
 * reaches `succeeded` AFTER the operator navigates away from the control plane still fires exactly one
 * full cache invalidation — closing the "navigate-away-mid-job" stale-after-success gap left by the
 * initial completion-driven cache-invalidation fix.
 *
 * `ControlPlaneView` is a pure CONSUMER (`useJobContext()`); forms keep reporting a started job through
 * `onStarted = reportJob`. The `useJob`/`useInvalidateOnJobSuccess` signatures in `../api/control` are
 * reused unchanged.
 */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

import { useInvalidateOnJobSuccess, useJob } from '../api/control';
import type { JobSnapshot } from '../api/types';

/** The context surface a control-plane consumer reads. */
export interface JobContextValue {
  /** The single active job id (rendered by a form's own JobPanel), or null. */
  activeJobId: string | null;
  /** The active job snapshot from the internal poll, or undefined until first fetched. */
  activeJob: JobSnapshot | undefined;
  /** True while the active job is `queued`/`running` — every start-action disables (single-writer). */
  busy: boolean;
  /** Report a freshly started job so the provider tracks busy + drives the terminal invalidation. */
  reportJob: (jobId: string) => void;
}

const JobCtx = createContext<JobContextValue | null>(null);

export function JobProvider({ children }: { children: ReactNode }) {
  const [activeJobId, setActiveJobId] = useState<string | null>(null);

  // Active-only poll of the single owned job. Reused unchanged from ../api/control.
  const { data: activeJob } = useJob(activeJobId);
  // When the owned job reaches `succeeded`, refresh the ENTIRE cache once. Living at the app
  // level (above <Routes>), this survives route changes — the whole point of lifting ownership here.
  useInvalidateOnJobSuccess(activeJob, activeJobId);

  const busy = activeJob?.status === 'queued' || activeJob?.status === 'running';

  // Stable across renders so a form's `onStarted` prop identity does not churn.
  const reportJob = useCallback((jobId: string) => setActiveJobId(jobId), []);

  const value = useMemo<JobContextValue>(
    () => ({ activeJobId, activeJob, busy, reportJob }),
    [activeJobId, activeJob, busy, reportJob],
  );

  return <JobCtx.Provider value={value}>{children}</JobCtx.Provider>;
}

/** Read the active-job context. Throws when called outside a `<JobProvider>`. */
export function useJobContext(): JobContextValue {
  const value = useContext(JobCtx);
  if (value === null) {
    throw new Error('useJobContext must be used within a JobProvider');
  }
  return value;
}
