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

/** Start a `generate` job → `202 {job_id}`. On success, refresh the tenant meta (`['summary']`). */
export function useStartGenerate(): UseMutationResult<{ job_id: string }, Error, GenerateArgs> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: GenerateArgs) => controlPost<{ job_id: string }>('/_control/generate', args),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['summary'] }),
  });
}

/**
 * Start an `analyze` job → `202 {job_id}`. On success ALSO invalidates `['control-profiles']` so the
 * freshly derived profile appears in the Generate PROFILE select (D-12).
 */
export function useStartAnalyze(): UseMutationResult<{ job_id: string }, Error, AnalyzeArgs> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: AnalyzeArgs) => controlPost<{ job_id: string }>('/_control/analyze', args),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['summary'] });
      void qc.invalidateQueries({ queryKey: ['control-profiles'] });
    },
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
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name }: { name: string }) =>
      controlPost<{ job_id: string }>(`/_control/snapshots/${encodeURIComponent(name)}/restore`, {}),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['summary'] });
      void qc.invalidateQueries({ queryKey: ['snapshots'] });
    },
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

/** Reset the active tenant to empty (`POST /_control/reset`) → `202 {job_id}` (CTRL-03, D-09). */
export function useReset(): UseMutationResult<{ job_id: string }, Error, void> {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => controlPost<{ job_id: string }>('/_control/reset', {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['summary'] }),
  });
}
