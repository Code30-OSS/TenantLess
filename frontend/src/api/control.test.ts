import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, type ReactNode } from 'react';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import {
  ArmError,
  controlPost,
  controlGet,
  controlDelete,
  setControlToken,
  getControlToken,
} from './client';
import {
  jobRefetchInterval,
  isAuthError,
  useStartGenerate,
  useStartAnalyze,
  useJob,
  useSources,
  useProfiles,
  useReset,
  useSaveSnapshot,
  useRestoreSnapshot,
  useDeleteSnapshot,
  useInvalidateOnJobSuccess,
} from './control';
import type { GenerateArgs, JobSnapshot } from './types';

/**
 * The control-plane data contract (CTRL-01 / CTRL-05, Phase 17) — the app's FIRST write surface.
 *
 * Task-1 pins the security-critical rule the whole phase rests on: the `X-Control-Token` is a
 * subprocess-spawning secret held in a SINGLE in-memory module variable — never localStorage, never
 * a cookie, never a URL. `controlPost`/`controlGet` reuse the exact same `assertSameOrigin` (WR-01
 * CSRF guard) + `toArmError` ({error:{code,message}} parse) primitives as `armGet`/`simGet`; only the
 * auth header differs (`X-Control-Token`, not the ARM Bearer).
 */

const fetchMock = vi.fn();

function okJson(body: unknown) {
  return { ok: true, status: 200, statusText: 'OK', json: async () => body } as Response;
}

function errJson(status: number, body: unknown) {
  return {
    ok: false,
    status,
    statusText: 'Error',
    json: async () => body,
  } as unknown as Response;
}

/**
 * A REAL 204 No-Content response (the snapshot-DELETE contract): no body, and `.json()`
 * throws — exactly like a browser `Response` built from a null body. Calling `.json()` on this
 * is the P2 bug (`controlDelete` used to always parse), so a faithful mock must throw here.
 */
function noContent() {
  return {
    ok: true,
    status: 204,
    statusText: 'No Content',
    json: async () => {
      throw new SyntaxError('Unexpected end of JSON input');
    },
  } as unknown as Response;
}

/** A fresh QueryClient + provider wrapper, returning the client so tests can inspect the cache. */
function hookWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
  return { wrapper, qc };
}

const GEN_ARGS: GenerateArgs = {
  profile: 'small',
  seed: 7,
  resources: 1000,
  subscriptions: 10,
  jobs: 4,
  violations: true,
  over_privilege: false,
};

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockReset();
  // Every test starts from a locked (no-token) control plane.
  setControlToken(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
  setControlToken(null);
});

describe('control token — in-memory only (CTRL-05, T-17-05)', () => {
  it('setControlToken/getControlToken round-trips the token in memory', () => {
    setControlToken('super-secret');
    expect(getControlToken()).toBe('super-secret');
  });

  it('never writes the token to localStorage or document.cookie', () => {
    setControlToken('super-secret');

    expect(window.localStorage.getItem('control-token')).toBeNull();
    expect(window.localStorage.getItem('controlToken')).toBeNull();
    expect(window.localStorage.getItem('X-Control-Token')).toBeNull();
    // No localStorage key ANYWHERE holds the secret value.
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const key = window.localStorage.key(i) as string;
      expect(window.localStorage.getItem(key)).not.toBe('super-secret');
    }
    expect(document.cookie).not.toContain('super-secret');
  });

  it('setControlToken(null) clears the in-memory token (401/403 lock path)', () => {
    setControlToken('super-secret');
    setControlToken(null);
    expect(getControlToken()).toBeNull();
  });

  it('a fresh module load starts with a null token (reset on reload — memory only)', async () => {
    setControlToken('will-be-gone');
    vi.resetModules();
    const fresh = await import('./client');
    expect(fresh.getControlToken()).toBeNull();
  });
});

describe('controlPost — attaches the in-memory X-Control-Token (CTRL-01)', () => {
  it('sends X-Control-Token + Content-Type and the JSON body when a token is set', async () => {
    setControlToken('s');
    fetchMock.mockResolvedValueOnce(okJson({ job_id: 'j1' }));

    const out = await controlPost<{ job_id: string }>('/_control/generate', { profile: 'small' });
    expect(out.job_id).toBe('j1');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/_control/generate');
    expect((init as RequestInit).method).toBe('POST');
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers['X-Control-Token']).toBe('s');
    expect(headers['Content-Type']).toBe('application/json');
    expect((init as RequestInit).body).toBe(JSON.stringify({ profile: 'small' }));
  });

  it('omits X-Control-Token entirely when the token is null', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ job_id: 'j2' }));
    await controlPost('/_control/generate', {});

    const [, init] = fetchMock.mock.calls[0];
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers['X-Control-Token']).toBeUndefined();
    // Content-Type is still present (the body is JSON).
    expect(headers['Content-Type']).toBe('application/json');
  });

  it('parses a non-2xx body into an ArmError via toArmError (reused ApiError parse)', async () => {
    setControlToken('s');
    fetchMock.mockResolvedValueOnce(
      errJson(401, { error: { code: 'InvalidControlToken', message: 'Invalid control token.' } }),
    );
    const err = (await controlPost('/_control/generate', {}).catch((e) => e)) as ArmError;
    expect(err).toBeInstanceOf(ArmError);
    expect(err.code).toBe('InvalidControlToken');
    expect(err.status).toBe(401);
  });
});

describe('controlDelete — resolves on a 204 No-Content body (P2 regression)', () => {
  it('resolves (never calls .json()) on a real 204 delete response', async () => {
    setControlToken('s');
    fetchMock.mockResolvedValueOnce(noContent());

    // The backend DELETE /_control/snapshots/{name} returns 204 with NO body; parsing it
    // (the old always-`.json()` path) throws → the delete would spuriously reject.
    await expect(controlDelete('/_control/snapshots/old')).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/_control/snapshots/old');
    expect((init as RequestInit).method).toBe('DELETE');
  });

  it('still throws an ArmError on a non-2xx delete', async () => {
    setControlToken('s');
    fetchMock.mockResolvedValueOnce(
      errJson(404, { error: { code: 'NotFound', message: "snapshot 'x' not found" } }),
    );
    const err = (await controlDelete('/_control/snapshots/x').catch((e) => e)) as ArmError;
    expect(err).toBeInstanceOf(ArmError);
    expect(err.status).toBe(404);
  });
});

describe('controlGet — attaches the in-memory X-Control-Token (CTRL-02)', () => {
  it('sends X-Control-Token when set and returns the parsed body', async () => {
    setControlToken('tok');
    fetchMock.mockResolvedValueOnce(okJson({ status: 'running', log: [] }));

    const out = await controlGet<{ status: string }>('/_control/jobs/abc');
    expect(out.status).toBe('running');

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/_control/jobs/abc');
    const headers = (init as RequestInit).headers as Record<string, string>;
    expect(headers['X-Control-Token']).toBe('tok');
  });
});

describe('control fetch — same-origin guard (WR-01, T-17-09 CSRF): fail closed', () => {
  it('controlPost rejects a protocol-relative //evil URL WITHOUT fetching', async () => {
    await expect(controlPost('//evil.example/x', {})).rejects.toBeInstanceOf(ArmError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('controlPost rejects an absolute cross-origin URL WITHOUT fetching', async () => {
    await expect(controlPost('https://attacker.example/_control/generate', {})).rejects.toBeInstanceOf(
      ArmError,
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('controlGet rejects a cross-origin URL WITHOUT fetching', async () => {
    await expect(controlGet('//evil.example/_control/jobs/1')).rejects.toBeInstanceOf(ArmError);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('jobRefetchInterval — active-only poll (D-07, CTRL-02)', () => {
  it('returns 1500ms while queued/running', () => {
    expect(jobRefetchInterval('queued')).toBe(1500);
    expect(jobRefetchInterval('running')).toBe(1500);
  });

  it('returns false (stops polling) when terminal or unknown', () => {
    expect(jobRefetchInterval('succeeded')).toBe(false);
    expect(jobRefetchInterval('failed')).toBe(false);
    expect(jobRefetchInterval(undefined)).toBe(false);
  });
});

describe('isAuthError — 401/403 drops to the token gate (CTRL-05)', () => {
  it('is true only for an ArmError with status 401/403', () => {
    expect(isAuthError(new ArmError('InvalidControlToken', 'x', 401))).toBe(true);
    expect(isAuthError(new ArmError('Forbidden', 'x', 403))).toBe(true);
    expect(isAuthError(new ArmError('InvalidRequestContent', 'x', 400))).toBe(false);
    expect(isAuthError(new Error('network'))).toBe(false);
    expect(isAuthError('nope')).toBe(false);
  });
});

describe('useStartGenerate / useStartAnalyze — the app FIRST useMutation (CTRL-01)', () => {
  it('useStartGenerate POSTs /_control/generate with the args and does NOT invalidate at submit', async () => {
    setControlToken('s');
    fetchMock.mockResolvedValue(okJson({ job_id: 'j1' }));
    const { wrapper, qc } = hookWrapper();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    const { result } = renderHook(() => useStartGenerate(), { wrapper });
    result.current.mutate(GEN_ARGS);
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock).toHaveBeenCalledWith(
      '/_control/generate',
      expect.objectContaining({ method: 'POST' }),
    );
    const [, init] = fetchMock.mock.calls[0];
    expect((init as RequestInit).body).toBe(JSON.stringify(GEN_ARGS));
    // Submit only ACCEPTS the job (tenant not yet changed) — the refresh is completion-driven.
    expect(invalidate).not.toHaveBeenCalled();
  });

  it('useStartAnalyze POSTs /_control/analyze and does NOT invalidate at submit (D-12)', async () => {
    setControlToken('s');
    fetchMock.mockResolvedValue(okJson({ job_id: 'j2' }));
    const { wrapper, qc } = hookWrapper();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    const { result } = renderHook(() => useStartAnalyze(), { wrapper });
    result.current.mutate({ source: 'sample-scan', out_name: 'derived-1' });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock).toHaveBeenCalledWith(
      '/_control/analyze',
      expect.objectContaining({ method: 'POST' }),
    );
    // The freshly derived profile now appears in the PROFILE select via the completion full-invalidate
    // (useInvalidateOnJobSuccess, covered above), NOT via a submit-time ['control-profiles'] invalidation.
    expect(invalidate).not.toHaveBeenCalled();
  });
});

describe('useInvalidateOnJobSuccess — completion-driven full refresh', () => {
  const succeeded = (): JobSnapshot => ({ status: 'succeeded', log: [] });
  const queued = (): JobSnapshot => ({ status: 'queued', log: [] });

  it('a succeeded job triggers exactly ONE full (no-arg) invalidateQueries (test 2)', () => {
    const { wrapper, qc } = hookWrapper();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    renderHook(
      ({ job, id }: { job: JobSnapshot | undefined; id: string | null }) =>
        useInvalidateOnJobSuccess(job, id),
      { wrapper, initialProps: { job: succeeded(), id: 'job-1' } },
    );

    expect(invalidate).toHaveBeenCalledTimes(1);
    // Full/all-families invalidation — the SAME shape as the Topbar Refresh: NO queryKey argument.
    expect(invalidate).toHaveBeenCalledWith();
  });

  it('never re-invalidates for the SAME succeeded job id across rerenders (ref guard, test 3)', () => {
    const { wrapper, qc } = hookWrapper();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    const { rerender } = renderHook(
      ({ job, id }: { job: JobSnapshot | undefined; id: string | null }) =>
        useInvalidateOnJobSuccess(job, id),
      { wrapper, initialProps: { job: succeeded(), id: 'job-1' } },
    );

    // The `useRef` guard is idempotent across ordinary re-renders — e.g. an active-poll refetch that
    // re-reads the SAME succeeded job id, or any unrelated parent rerender. (No StrictMode effect
    // replay is exercised here; a plain `rerender` is a re-render, not a mount/unmount/remount.)
    rerender({ job: succeeded(), id: 'job-1' });
    rerender({ job: succeeded(), id: 'job-1' });

    expect(invalidate).toHaveBeenCalledTimes(1);
  });

  it('a later, DIFFERENT succeeded job id invalidates again (test 4)', () => {
    const { wrapper, qc } = hookWrapper();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    const { rerender } = renderHook(
      ({ job, id }: { job: JobSnapshot | undefined; id: string | null }) =>
        useInvalidateOnJobSuccess(job, id),
      { wrapper, initialProps: { job: succeeded(), id: 'job-1' } },
    );

    rerender({ job: succeeded(), id: 'job-2' });

    expect(invalidate).toHaveBeenCalledTimes(2);
  });

  it('non-terminal / undefined states are NO-OPS (queued or no job → never invalidates)', () => {
    const { wrapper, qc } = hookWrapper();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    const { rerender } = renderHook(
      ({ job, id }: { job: JobSnapshot | undefined; id: string | null }) =>
        useInvalidateOnJobSuccess(job, id),
      { wrapper, initialProps: { job: undefined, id: null } },
    );

    rerender({ job: queued(), id: 'job-1' });
    rerender({ job: { status: 'running', log: [] }, id: 'job-1' });

    expect(invalidate).not.toHaveBeenCalled();
  });
});

describe('useJob — active-only poll; token never in the query key (CTRL-02 / T-17-05)', () => {
  it('queryKey is exactly [job, id] — the token is absent from the key', async () => {
    setControlToken('super-secret');
    fetchMock.mockResolvedValue(okJson({ status: 'succeeded', log: [] }));
    const { wrapper, qc } = hookWrapper();

    renderHook(() => useJob('abc'), { wrapper });
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    expect(fetchMock).toHaveBeenCalledWith('/_control/jobs/abc', expect.anything());
    const keys = qc.getQueryCache().getAll().map((q) => q.queryKey);
    expect(keys).toContainEqual(['job', 'abc']);
    expect(JSON.stringify(keys)).not.toContain('super-secret');
  });

  it('useJob(null) stays disabled — never fetches', async () => {
    const { wrapper } = hookWrapper();
    renderHook(() => useJob(null), { wrapper });
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('useSources / useProfiles — server-populated selects (CTRL-01, D-12)', () => {
  it('useSources GETs /_control/sources, key [control-sources], token not in key', async () => {
    setControlToken('tok');
    fetchMock.mockResolvedValue(okJson({ sources: [{ name: 'sample-scan' }] }));
    const { wrapper, qc } = hookWrapper();

    const { result } = renderHook(() => useSources(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock).toHaveBeenCalledWith('/_control/sources', expect.anything());
    expect(result.current.data).toEqual([{ name: 'sample-scan' }]);
    const keys = qc.getQueryCache().getAll().map((q) => q.queryKey);
    expect(keys).toContainEqual(['control-sources']);
    expect(JSON.stringify(keys)).not.toContain('tok');
  });

  it('useProfiles GETs /_control/profiles, key [control-profiles]', async () => {
    setControlToken('tok');
    fetchMock.mockResolvedValue(okJson({ profiles: [{ name: 'enterprise' }, { name: 'small' }] }));
    const { wrapper, qc } = hookWrapper();

    const { result } = renderHook(() => useProfiles(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock).toHaveBeenCalledWith('/_control/profiles', expect.anything());
    expect(result.current.data).toEqual([{ name: 'enterprise' }, { name: 'small' }]);
    const keys = qc.getQueryCache().getAll().map((q) => q.queryKey);
    expect(keys).toContainEqual(['control-profiles']);
  });

  it('useSources stays disabled when the control plane is locked (no token)', async () => {
    setControlToken(null);
    const { wrapper } = hookWrapper();
    renderHook(() => useSources(), { wrapper });
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('reset + snapshot mutations (17-04 endpoints)', () => {
  it('useReset POSTs /_control/reset and does NOT invalidate at submit', async () => {
    setControlToken('s');
    fetchMock.mockResolvedValue(okJson({ job_id: 'r1' }));
    const { wrapper, qc } = hookWrapper();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    const { result } = renderHook(() => useReset(), { wrapper });
    result.current.mutate();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock).toHaveBeenCalledWith(
      '/_control/reset',
      expect.objectContaining({ method: 'POST' }),
    );
    // Reset is a 202 job — the Explorer refreshes on its `succeeded` transition, not at submit.
    expect(invalidate).not.toHaveBeenCalled();
  });

  it('useSaveSnapshot POSTs /_control/snapshots and invalidates [snapshots]', async () => {
    setControlToken('s');
    fetchMock.mockResolvedValue(okJson({ job_id: 's1' }));
    const { wrapper, qc } = hookWrapper();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    const { result } = renderHook(() => useSaveSnapshot(), { wrapper });
    result.current.mutate({ name: 'nightly' });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock).toHaveBeenCalledWith(
      '/_control/snapshots',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['snapshots'] });
  });

  it('useRestoreSnapshot POSTs /_control/snapshots/{name}/restore (name encoded) and does NOT invalidate at submit', async () => {
    setControlToken('s');
    fetchMock.mockResolvedValue(okJson({ job_id: 's2' }));
    const { wrapper, qc } = hookWrapper();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    const { result } = renderHook(() => useRestoreSnapshot(), { wrapper });
    result.current.mutate({ name: 'a b' });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock).toHaveBeenCalledWith(
      '/_control/snapshots/a%20b/restore',
      expect.objectContaining({ method: 'POST' }),
    );
    // Restore is a 202 hot-swap job — the Explorer refreshes on its `succeeded` transition, not at submit.
    expect(invalidate).not.toHaveBeenCalled();
  });

  it('useDeleteSnapshot DELETEs /_control/snapshots/{name} and succeeds on a real 204', async () => {
    setControlToken('s');
    // The real backend returns 204 No-Content (not a JSON body); the mutation must succeed.
    fetchMock.mockResolvedValue(noContent());
    const { wrapper } = hookWrapper();

    const { result } = renderHook(() => useDeleteSnapshot(), { wrapper });
    result.current.mutate({ name: 'old' });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock).toHaveBeenCalledWith(
      '/_control/snapshots/old',
      expect.objectContaining({ method: 'DELETE' }),
    );
  });
});
