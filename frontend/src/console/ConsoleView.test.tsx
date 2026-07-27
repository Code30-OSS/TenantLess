import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import ConsoleView from './ConsoleView';
import type { RequestEvent } from '../api/console';

/**
 * CONS-01..04 — the ConsoleView integration smoke test. The deep behaviors (filter math, scale math,
 * the SSE hook, the SVG charts, the feed + inspector) are already pinned by the wave-1/2 unit + component
 * suites; this shallow integration test proves the WIRING: the one-shot `/stats`+`/history` fetch seeds
 * the buffer, the H1 + latency-history + by-route/by-status blocks mount, and toggling ONE shared status
 * filter re-filters the feed (Pattern 7 — one filter, four consumers).
 *
 * `fetch` and the global `EventSource` are both mocked (the EventSource idiom is reused from
 * useEventStream.test.ts). The stream must be flipped to `live` (emitOpen) before the feed renders rows.
 */

const NOW = Date.now();

function ev(path: string, status: number): RequestEvent {
  return { ts_ms: NOW, method: 'GET', path, route: '/r/x', status, latency_ms: status >= 500 ? 9 : 3 };
}

const STATS = {
  total: 2,
  by_status: { '200': 1, '503': 1 },
  by_route: { '/r/x': 2 },
  p50_ms: 3,
  p95_ms: 9,
  max_ms: 9,
  recent: [ev('/p/500', 503), ev('/p/200', 200)], // newest-first
};

const HISTORY = { bucket_ms: 1000, window_ms: 300_000, server_now_ms: NOW, buckets: [] };

class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  closed = 0;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }
  close(): void {
    this.closed += 1;
  }
  emitOpen(): void {
    act(() => this.onopen?.(new Event('open')));
  }
  static get last(): MockEventSource {
    return MockEventSource.instances[MockEventSource.instances.length - 1];
  }
}

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal('EventSource', MockEventSource);
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string | URL) => {
      const u = String(url);
      if (u.includes('/_console/stats')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(STATS) } as Response);
      }
      if (u.includes('/_console/history')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(HISTORY) } as Response);
      }
      return Promise.reject(new Error(`unexpected fetch: ${u}`));
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderConsole() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ConsoleView />
    </QueryClientProvider>,
  );
}

describe('ConsoleView — composition + wiring', () => {
  it('renders the Observability Console H1 (visible even before /stats resolves)', () => {
    renderConsole();
    expect(screen.getByRole('heading', { name: 'Observability Console' })).toBeTruthy();
  });

  it('mounts the latency-history + by-route + by-status blocks once /stats resolves', async () => {
    renderConsole();
    await waitFor(() => expect(screen.getByText('◆ Latency history')).toBeTruthy());
    expect(screen.getByText('◆ By route')).toBeTruthy();
    expect(screen.getByText('◆ By status')).toBeTruthy();
  });

  it('seeds the feed from /stats.recent and re-filters it when a shared status chip toggles', async () => {
    renderConsole();

    // Wait for /stats to resolve → ConsoleBody mounts → useEventStream opens the (mock) socket.
    await waitFor(() => expect(MockEventSource.instances.length).toBeGreaterThan(0));
    MockEventSource.last.emitOpen(); // flip 'connecting' → 'live' so the feed renders rows

    // Both seeded rows are visible (default filter: no status, 5m window).
    await waitFor(() => expect(screen.getByText('/p/200')).toBeTruthy());
    expect(screen.getByText('/p/500')).toBeTruthy();

    // Toggle the shared 5xx status chip → the ONE filter re-filters the feed to the 503 event only.
    fireEvent.click(screen.getByRole('button', { name: '5xx' }));

    await waitFor(() => expect(screen.queryByText('/p/200')).toBeNull());
    expect(screen.getByText('/p/500')).toBeTruthy();
  });
});
