import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, type ReactNode } from 'react';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { ArmError } from './client';
import {
  consoleGet,
  useConsoleStats,
  useConsoleHistory,
  HISTORY_REFETCH_MS,
  STATUS_TOKEN,
  type HistorySnapshot,
} from './console';

/**
 * The bearer-EXEMPT Console data contract (CONS-01 / CONS-04). Pins BOTH threat mitigations of this
 * layer: (T-16-01) every `/_console/**` fetch is same-origin AND carries NO `Authorization` header —
 * a Bearer must never leak to a console route; (WR-01) `consoleGet` fails closed on a cross-origin
 * path via the reused `assertSameOrigin` guard. Also pins the exact `/_console/stats` + `/_console/
 * history` paths the hooks request and that `STATUS_TOKEN` is a token-name-only map (no raw hex).
 */

const fetchMock = vi.fn();

function okJson(body: unknown) {
  return { ok: true, status: 200, statusText: 'OK', json: async () => body } as Response;
}

function hookWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('consoleGet — same-origin guard (WR-01): fail closed on non-relative paths', () => {
  it('rejects an absolute cross-origin URL WITHOUT issuing a fetch', async () => {
    await expect(consoleGet('https://attacker.example/_console/stats')).rejects.toBeInstanceOf(
      ArmError,
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects a protocol-relative //evil/_console URL WITHOUT fetching', async () => {
    await expect(consoleGet('//evil.example/_console/stats')).rejects.toBeInstanceOf(ArmError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('fetches a legitimate relative /_console path with NO Authorization header (bearer-exempt)', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ total: 0 }));
    await consoleGet('/_console/stats');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/_console/stats');
    const [, init] = fetchMock.mock.calls[0];
    // simGet passes fetch(path) with no init at all — no headers object, no Bearer.
    expect((init as RequestInit | undefined)?.headers).toBeUndefined();
  });
});

describe('useConsoleStats — one-shot /_console/stats seed (bearer-exempt, Pitfall 3)', () => {
  it('fires consoleGet against the exact /_console/stats path with NO auth header', async () => {
    fetchMock.mockResolvedValue(okJson({ total: 3, by_status: {}, by_route: {}, recent: [] }));
    renderHook(() => useConsoleStats(), { wrapper: hookWrapper() });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/_console/stats'));
    const [, init] = fetchMock.mock.calls[0];
    expect((init as RequestInit | undefined)?.headers).toBeUndefined();
  });
});

describe('useConsoleHistory — one-shot /_console/history series (bearer-exempt)', () => {
  it('fires consoleGet against the exact /_console/history path with NO auth header', async () => {
    const body: HistorySnapshot = {
      bucket_ms: 1000,
      window_ms: 300_000,
      server_now_ms: 1_720_358_400_000,
      buckets: [{ ts_ms: 1_720_358_100_000, count: 0, p50_ms: null, p95_ms: null, max_ms: null }],
    };
    fetchMock.mockResolvedValue(okJson(body));
    renderHook(() => useConsoleHistory(), { wrapper: hookWrapper() });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/_console/history'));
    const [, init] = fetchMock.mock.calls[0];
    expect((init as RequestInit | undefined)?.headers).toBeUndefined();
  });

  it('re-polls on an interval so the live latency chart advances (not a one-shot freeze)', async () => {
    const body: HistorySnapshot = {
      bucket_ms: 1000,
      window_ms: 300_000,
      server_now_ms: 1_720_358_400_000,
      buckets: [],
    };
    fetchMock.mockResolvedValue(okJson(body));
    vi.useFakeTimers();
    try {
      renderHook(() => useConsoleHistory(), { wrapper: hookWrapper() });
      await vi.advanceTimersByTimeAsync(0); // flush the mount fetch
      expect(fetchMock).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(HISTORY_REFETCH_MS + 50); // one poll cycle
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('STATUS_TOKEN — status class → design-token names only (no raw hex)', () => {
  it('maps each status class to a --token name', () => {
    expect(STATUS_TOKEN['2xx']).toBe('--green');
    expect(STATUS_TOKEN['3xx']).toBe('--text-2');
    expect(STATUS_TOKEN['4xx']).toBe('--amber');
    expect(STATUS_TOKEN['5xx']).toBe('--red');
  });

  it('contains no raw hex color literals (token-only discipline, D-01)', () => {
    for (const token of Object.values(STATUS_TOKEN)) {
      expect(token.startsWith('--')).toBe(true);
      expect(/#[0-9a-fA-F]{3,6}/.test(token)).toBe(false);
    }
  });
});
