import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createElement, useEffect, type ReactNode } from 'react';
import { render, renderHook, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { JobProvider, useJobContext } from './JobContext';

/**
 * JobContext — the app-level job owner (lifecycle half).
 *
 * THE regression this file pins: the completion-driven full invalidation must survive the
 * ControlPlaneView unmount. In the OLD architecture the `useJob` poll + `useInvalidateOnJobSuccess`
 * watcher lived INSIDE ControlPlaneView, so navigating to Explorer (unmounting the view) before a job
 * finished stopped the poll and dropped `activeJobId` — a job that succeeded after navigation never
 * invalidated the `staleTime: Infinity` cache. Lifting ownership to a `JobProvider` mounted above
 * `<Routes>` fixes it: the provider never unmounts on route change, so the terminal invalidation still
 * fires exactly once even after the reporting child is gone.
 */

// VITEST HOISTING: `vi.mock` is hoisted above every top-level declaration and REJECTS a factory that
// closes over a non-`mock`-prefixed variable. The controllable job snapshot therefore MUST come from
// `vi.hoisted` (a bare module-level `jobState` var would throw a hoisting error at collection time).
const { mockJob } = vi.hoisted(() => ({
  mockJob: { current: { status: 'queued', log: [] } } as {
    current: { status: string; log: string[] } | undefined;
  },
}));

// Partial mock: keep the REAL `useInvalidateOnJobSuccess` (that watcher is under test), but return a
// controllable snapshot from `useJob` so we can drive the job to `succeeded` on command.
vi.mock('../api/control', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/control')>();
  return { ...actual, useJob: () => ({ data: mockJob.current }) };
});

/** A fresh QueryClient + provider wrapper (control.test.ts's `hookWrapper()` idiom). */
function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
  return { qc, wrapper };
}

/** On mount, report a started job through the context — simulating a form's `onStarted`. */
function ReportingChild() {
  const { reportJob } = useJobContext();
  useEffect(() => {
    reportJob('job-1');
  }, [reportJob]);
  return null;
}

/** A leaf that surfaces `busy` for assertion. */
function BusyProbe() {
  const { busy } = useJobContext();
  return <span data-testid="busy">{String(busy)}</span>;
}

beforeEach(() => {
  mockJob.current = { status: 'queued', log: [] };
});

describe('JobProvider — survives-unmount terminal invalidation (lifecycle)', () => {
  it('fires exactly one full invalidation when the job succeeds AFTER the reporting child unmounts', () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    });
    const invalidate = vi.spyOn(qc, 'invalidateQueries');

    // First render: the child reports job-1; the job is queued.
    const { rerender, getByTestId } = render(
      <QueryClientProvider client={qc}>
        <JobProvider>
          <ReportingChild />
          <BusyProbe />
        </JobProvider>
      </QueryClientProvider>,
    );

    // Queued → busy, and NO invalidation yet.
    expect(getByTestId('busy').textContent).toBe('true');
    expect(invalidate).not.toHaveBeenCalled();

    // The job succeeds AND the reporting child unmounts (operator navigated to Explorer). The provider
    // — being the wrapper, not the child — stays mounted and re-reads the now-succeeded job.
    act(() => {
      mockJob.current = { status: 'succeeded', log: [] };
    });
    rerender(
      <QueryClientProvider client={qc}>
        <JobProvider>
          <BusyProbe />
        </JobProvider>
      </QueryClientProvider>,
    );

    // The watcher survived the child unmount → exactly one full (no-arg) invalidation. On the old
    // watcher-in-child architecture this is zero calls, and this assertion fails.
    expect(invalidate).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalledWith();
    // And the provider now reports not-busy (succeeded is terminal).
    expect(getByTestId('busy').textContent).toBe('false');
  });
});

describe('useJobContext — provider guard', () => {
  it('throws when used outside a JobProvider', () => {
    // Silence the expected React error-boundary console noise from the throw.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => renderHook(() => useJobContext())).toThrow(/must be used within a JobProvider/);
    spy.mockRestore();
  });
});

describe('JobProvider — busy derivation', () => {
  it('is true while the active job is running', () => {
    mockJob.current = { status: 'running', log: [] };
    const { wrapper } = makeWrapper();
    const { getByTestId } = render(
      <JobProvider>
        <ReportingChild />
        <BusyProbe />
      </JobProvider>,
      { wrapper },
    );
    expect(getByTestId('busy').textContent).toBe('true');
  });

  it('is false when the active job has succeeded', () => {
    mockJob.current = { status: 'succeeded', log: [] };
    const { wrapper } = makeWrapper();
    const { getByTestId } = render(
      <JobProvider>
        <ReportingChild />
        <BusyProbe />
      </JobProvider>,
      { wrapper },
    );
    expect(getByTestId('busy').textContent).toBe('false');
  });

  it('is false when there is no job snapshot (undefined)', () => {
    mockJob.current = undefined;
    const { wrapper } = makeWrapper();
    const { getByTestId } = render(
      <JobProvider>
        <BusyProbe />
      </JobProvider>,
      { wrapper },
    );
    expect(getByTestId('busy').textContent).toBe('false');
  });
});
