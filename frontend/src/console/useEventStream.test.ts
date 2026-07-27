import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import { useEventStream } from './useEventStream';
import type { RequestEvent } from '../api/console';

/**
 * The native-`EventSource` live-feed hook (CONS-04, RESEARCH Pattern 4). A mock `EventSource` on
 * `globalThis` lets us drive synthetic open/message/error events without a socket. Pins the four
 * load-bearing behaviors: (1) status `connecting → live` on open; (2) newest-first PREPEND bounded to
 * `cap` (memory-safe buffer, T-16-05); (3) pause HOLDS — drops from view but keeps the socket OPEN;
 * (4) `close()` runs exactly once on unmount (SSE-leak mitigation, T-16-05). Plus: same-origin
 * root-relative stream URL, and a one-shot `seed` (Pitfall 3 — no repeated full-`recent` poll).
 */

function ev(ts_ms: number, status = 200): RequestEvent {
  return { ts_ms, method: 'GET', path: `/p/${ts_ms}`, route: '/p/{id}', status, latency_ms: 5 };
}

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

  // --- test drivers ---
  emitOpen(): void {
    act(() => this.onopen?.(new Event('open')));
  }
  emitError(): void {
    act(() => this.onerror?.(new Event('error')));
  }
  emitMessage(data: RequestEvent): void {
    act(() => this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent));
  }

  static get last(): MockEventSource {
    return MockEventSource.instances[MockEventSource.instances.length - 1];
  }
}

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal('EventSource', MockEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useEventStream — connection + same-origin stream URL', () => {
  it('opens a root-relative same-origin EventSource on /_console/stream (no absolute origin)', () => {
    renderHook(() => useEventStream(10));
    const es = MockEventSource.last;
    expect(es.url).toBe('/_console/stream');
    // Root-relative only — never a protocol-relative or absolute cross-origin URL.
    expect(es.url.startsWith('/')).toBe(true);
    expect(es.url.startsWith('//')).toBe(false);
    expect(es.url).not.toMatch(/^https?:\/\//);
  });

  it('starts connecting, flips to live on open and reconnecting on error', () => {
    const { result } = renderHook(() => useEventStream(10));
    expect(result.current.status).toBe('connecting');
    MockEventSource.last.emitOpen();
    expect(result.current.status).toBe('live');
    MockEventSource.last.emitError();
    expect(result.current.status).toBe('reconnecting');
  });
});

describe('useEventStream — bounded, newest-first buffer', () => {
  it('prepends each message newest-first and never exceeds cap', () => {
    const { result } = renderHook(() => useEventStream(2));
    const es = MockEventSource.last;
    es.emitMessage(ev(1));
    es.emitMessage(ev(2));
    es.emitMessage(ev(3));

    expect(result.current.events).toHaveLength(2); // bounded at cap
    expect(result.current.events.map((e) => e.ts_ms)).toEqual([3, 2]); // newest-first
  });
});

describe('useEventStream — one-shot seed (Pitfall 3)', () => {
  it('seeds the buffer once from the passed-in newest-first slice', () => {
    const seed = [ev(30), ev(20), ev(10)];
    const { result } = renderHook(() => useEventStream(5, seed));
    expect(result.current.events.map((e) => e.ts_ms)).toEqual([30, 20, 10]);

    MockEventSource.last.emitMessage(ev(40));
    expect(result.current.events.map((e) => e.ts_ms)).toEqual([40, 30, 20, 10]);
  });
});

describe('useEventStream — pause holds without closing the socket', () => {
  it('drops a message from the view while paused but keeps the socket OPEN', () => {
    const { result } = renderHook(() => useEventStream(10));
    const es = MockEventSource.last;

    es.emitMessage(ev(1));
    act(() => result.current.pause(true));
    es.emitMessage(ev(2)); // dropped from view while paused

    expect(result.current.events.map((e) => e.ts_ms)).toEqual([1]);
    expect(es.closed).toBe(0); // pause is a HOLD, not a close

    act(() => result.current.pause(false));
    es.emitMessage(ev(3));
    expect(result.current.events.map((e) => e.ts_ms)).toEqual([3, 1]);
  });
});

describe('useEventStream — leak-safe cleanup (T-16-05)', () => {
  it('closes the socket exactly once on unmount and NOT on pause', () => {
    const { result, unmount } = renderHook(() => useEventStream(10));
    const es = MockEventSource.last;

    act(() => result.current.pause(true));
    expect(es.closed).toBe(0);

    unmount();
    expect(es.closed).toBe(1);
  });
});
