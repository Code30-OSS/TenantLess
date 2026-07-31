/**
 * The control-plane data layer (Phase 17, CTRL-01/CTRL-02/CTRL-05) — the app's FIRST write surface.
 *
 * This module mirrors the `api/queries.ts` hook idiom, but over the `/_control/*` routes behind the
 * distinct `X-Control-Token` realm (D-01/D-17). It introduces the app's first `useMutation`
 * (`useStartGenerate`/`useStartAnalyze`) and its first active-only poll (`useJob`) — everything else in
 * the app runs under the no-poll QueryClient (main.tsx: staleTime Infinity, refetch off), so polling is
 * strictly opt-in per hook.
 *
 * Security invariants pinned by `control.test.ts`:
 * - The control token lives ONLY in the in-memory module var owned by `client.ts` (re-exported here as
 *   `setControlToken`/`getControlToken`) — never localStorage, cookie, URL, log, or a TanStack
 *   `queryKey` (threat T-17-05). `useJob`'s key is `['job', id]` — the token is never a key component.
 * - Every fetch goes through `controlPost`/`controlGet`/`controlDelete`, which reuse `assertSameOrigin`
 *   (WR-01 CSRF) + `toArmError` (the single `{error:{code,message}}` parse).
 * - On a 401/403 the consuming view calls `setControlToken(null)` to re-lock the plane; `isAuthError`
 *   is the shared predicate so every surface drops to the token gate uniformly.
 */
import { useEffect, useRef } from 'react';
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';

import {
  ArmError,
  controlDelete,
  controlGet,
  controlPost,
  getControlToken,
  setControlToken,
} from './client';
import type {
  AnalyzeArgs,
  ControlProfile,
  ControlSource,
  GenerateArgs,
  JobSnapshot,
  JobStatus,
  Snapshot,
} from './types';

// Re-export the token + fetch primitives so a control-plane view has a single import surface.
export { controlGet, controlPost, getControlToken, setControlToken };

/** Job poll cadence (ms). Active-only (D-07) — the UI polls a job ONLY while it is queued/running. */
export const JOB_POLL_MS = 1500;

/**
 * The active-only refetch policy (D-07), factored out as a pure function so it is directly testable
 * without a live query: {@link JOB_POLL_MS} while the job is `queued`/`running`, else `false` (stop).
 */
export function jobRefetchInterval(status: JobStatus | undefined): number | false {
  return status === 'queued' || status === 'running' ? JOB_POLL_MS : false;
}

/**
 * The shared "drop to the token gate" predicate (CTRL-05): a control response `401`/`403` means the
 * in-memory token is invalid — the view clears it via `setControlToken(null)` and re-locks the plane.
 */
export function isAuthError(err: unknown): boolean {
  return err instanceof ArmError && (err.status === 401 || err.status === 403);
}

// ---------------------------------------------------------------------------
// Mutations — the app's FIRST useMutation (CTRL-01)
// ---------------------------------------------------------------------------

/**
 * Start a `generate` job → `202 {job_id}`. Submit only ACCEPTS the job — the tenant is not yet
 * changed, so there is NO submit-time invalidation (that was the stale-after-success bug). The
 * Explorer refreshes when the owned active job reaches `succeeded` via {@link useInvalidateOnJobSuccess}.
 */
export function useStartGenerate(): UseMutationResult<{ job_id: string }, Error, GenerateArgs> {
  return useMutation({
    mutationFn: (args: GenerateArgs) => controlPost<{ job_id: string }>('/_control/generate', args),
  });
}

/**
 * Start an `analyze` job → `202 {job_id}`. No submit-time invalidation: the freshly derived profile
 * appears in the Generate PROFILE select when the job reaches `succeeded` (the completion full-invalidate
 * in {@link useInvalidateOnJobSuccess} covers `['control-profiles']`), not at 202-submit (D-12).
 */
export function useStartAnalyze(): UseMutationResult<{ job_id: string }, Error, AnalyzeArgs> {
  return useMutation({
    mutationFn: (args: AnalyzeArgs) => controlPost<{ job_id: string }>('/_control/analyze', args),
  });
}

// ---------------------------------------------------------------------------
// Job poll (CTRL-02) — active-only refetch; token NEVER in the query key
// ---------------------------------------------------------------------------

/**
 * Poll a job snapshot (`GET /_control/jobs/{id}`). The `queryKey` is `['job', id]` — the token is never
 * a key component (T-17-05). Disabled until an `id` exists; polls every {@link JOB_POLL_MS} ONLY while
 * `queued`/`running`, then stops (D-07).
 */
export function useJob(id: string | null): UseQueryResult<JobSnapshot, Error> {
  return useQuery({
    queryKey: ['job', id],
    queryFn: () => controlGet<JobSnapshot>(`/_control/jobs/${id}`),
    enabled: Boolean(id),
    refetchInterval: (query) => jobRefetchInterval(query.state.data?.status),
  });
}

/**
 * Completion-driven full cache refresh ("stale-after-success"). Tenant-mutating control-plane
 * actions are async JOBS: their `202` submit only means "job accepted", NOT "tenant changed" — so a
 * submit-time invalidation is mistimed. This hook watches the SINGLE owned active job and, the moment
 * it reports `succeeded`, invalidates the ENTIRE cache with NO queryKey argument (identical to the
 * Topbar Refresh) so every Explorer family (`['summary']`, `['control-profiles']`, `['snapshots']`, …)
 * self-heals with no manual Refresh.
 *
 * Guarded by a `useRef` keyed on the job id so it fires EXACTLY ONCE per succeeded job — never a storm
 * from the active-only poll refetch, React StrictMode's double-invoke, or an unrelated rerender. A
 * later, different succeeded job id invalidates again. Non-terminal states (`queued`/`running`/`failed`/
 * `undefined`) are no-ops. Invoked by the app-level `JobProvider` (JobContext, mounted above <Routes>) —
 * the single holder of `activeJob` — so a job that succeeds after the operator navigates away still fires
 * exactly one invalidation; `ControlPlaneView` is a pure consumer.
 */
export function useInvalidateOnJobSuccess(job: JobSnapshot | undefined, jobId: string | null): void {
  const qc = useQueryClient();
  const invalidatedRef = useRef<string | null>(null);
  useEffect(() => {
    if (jobId && job?.status === 'succeeded' && invalidatedRef.current !== jobId) {
      invalidatedRef.current = jobId;
      void qc.invalidateQueries();
    }
  }, [job?.status, jobId, qc]);
}

// ---------------------------------------------------------------------------
// Server-populated selects (CTRL-01, D-12) — mirror useSnapshots' GET-list idiom
// ---------------------------------------------------------------------------

/** Allowlisted analyze sources (`GET /_control/sources`) for the AnalyzeForm SOURCE select. */
export function useSources(): UseQueryResult<ControlSource[], Error> {
  return useQuery({
    queryKey: ['control-sources'],
    queryFn: () =>
      controlGet<{ sources: ControlSource[] }>('/_control/sources').then((r) => r.sources ?? []),
    enabled: getControlToken() !== null,
  });
}

/** Generate-profile allowlist (`GET /_control/profiles`) for the GenerateForm PROFILE select. */
export function useProfiles(): UseQueryResult<ControlProfile[], Error> {
  return useQuery({
    queryKey: ['control-profiles'],
    queryFn: () =>
      controlGet<{ profiles: ControlProfile[] }>('/_control/profiles').then((r) => r.profiles ?? []),
    enabled: getControlToken() !== null,
  });
}

// ---------------------------------------------------------------------------
// Named snapshots + reset (CTRL-03/CTRL-04, 17-04 endpoints)
// ---------------------------------------------------------------------------

/** Named tenant snapshots (`GET /_control/snapshots`) for the TenantsManager list. */
export function useSnapshots(): UseQueryResult<Snapshot[], Error> {
  return useQuery({
    queryKey: ['snapshots'],
    queryFn: () =>
      controlGet<{ snapshots: Snapshot[] }>('/_control/snapshots').then((r) => r.snapshots ?? []),
    enabled: getControlToken() !== null,
  });
}

/** Save the active tenant as a named snapshot (`POST /_control/snapshots`) → `202 {job_id}`. */
export function useSaveSnapshot(): UseMutationResult<{ job_id: string }, Error, { name: string }> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: { name: string }) =>
      controlPost<{ job_id: string }>('/_control/snapshots', args),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['snapshots'] }),
  });
}

/**
 * Restore (hot-swap) a named snapshot (`POST /_control/snapshots/{name}/restore`) → `202 {job_id}`.
 * The name is `encodeURIComponent`-encoded so a value cannot inject an extra path segment (WR-02);
 * the server also safe-name-validates it.
 */
export function useRestoreSnapshot(): UseMutationResult<{ job_id: string }, Error, { name: string }> {
  return useMutation({
    mutationFn: ({ name }: { name: string }) =>
      controlPost<{ job_id: string }>(`/_control/snapshots/${encodeURIComponent(name)}/restore`, {}),
  });
}

/** Delete a named snapshot (`DELETE /_control/snapshots/{name}`). Name encoded (WR-02). */
export function useDeleteSnapshot(): UseMutationResult<unknown, Error, { name: string }> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name }: { name: string }) =>
      controlDelete(`/_control/snapshots/${encodeURIComponent(name)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['snapshots'] }),
  });
}

/**
 * Reset the active tenant to empty (`POST /_control/reset`) → `202 {job_id}` (CTRL-03, D-09). No
 * submit-time invalidation — the Explorer refreshes when the reset job reaches `succeeded`.
 */
export function useReset(): UseMutationResult<{ job_id: string }, Error, void> {
  return useMutation({
    mutationFn: () => controlPost<{ job_id: string }>('/_control/reset', {}),
  });
}
