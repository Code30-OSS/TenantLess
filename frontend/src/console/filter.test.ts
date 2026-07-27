import { describe, it, expect } from 'vitest';

import {
  statusClass,
  matches,
  windowCutoffMs,
  sliceByWindow,
  drilldownBuckets,
  WINDOW_MS,
  type ConsoleEvent,
  type ConsoleFilter,
} from './filter';

/** Build a RequestEvent-shaped fixture (mirrors mock-server RequestEvent). */
function ev(partial: Partial<ConsoleEvent>): ConsoleEvent {
  return {
    ts_ms: 0,
    method: 'GET',
    path: '/subscriptions/x/resources',
    route: '/subscriptions/{sub}/resources',
    status: 200,
    latency_ms: 5,
    ...partial,
  };
}

/** Build a ConsoleFilter with sensible defaults (pass-through). */
function filter(partial: Partial<ConsoleFilter> = {}): ConsoleFilter {
  return {
    status: new Set(),
    route: null,
    window: '5m',
    ...partial,
  };
}

describe('statusClass', () => {
  it('classes a status by its hundreds band (mirrors the server 100-band classing)', () => {
    expect(statusClass(204)).toBe('2xx');
    expect(statusClass(301)).toBe('3xx');
    expect(statusClass(404)).toBe('4xx');
    expect(statusClass(500)).toBe('5xx');
  });

  it('clamps out-of-range statuses into the 2xx–5xx bands (never an invalid class)', () => {
    expect(statusClass(599)).toBe('5xx');
    expect(statusClass(100)).toBe('2xx');
  });
});

describe('matches', () => {
  it('includes an event on a status-class + route hit', () => {
    const f = filter({ status: new Set(['4xx']), route: '/subscriptions/{sub}/resources' });
    expect(matches(ev({ status: 404 }), f)).toBe(true);
  });

  it('excludes an event when the route does not match the selected route', () => {
    const f = filter({ route: '/subscriptions/{sub}/resources' });
    expect(matches(ev({ route: '/subscriptions' }), f)).toBe(false);
  });

  it('excludes an event when its status class is not in a non-empty status set', () => {
    const f = filter({ status: new Set(['5xx']) });
    expect(matches(ev({ status: 200 }), f)).toBe(false);
  });

  it('passes through ANY event on an empty filter (empty status set + null route)', () => {
    const f = filter();
    expect(matches(ev({ status: 500, route: '/anything' }), f)).toBe(true);
    expect(matches(ev({ status: 204, route: '/other' }), f)).toBe(true);
  });

  it('treats a multi-select status set as OR (any class in the set passes)', () => {
    const f = filter({ status: new Set(['4xx', '5xx']) });
    expect(matches(ev({ status: 404 }), f)).toBe(true);
    expect(matches(ev({ status: 503 }), f)).toBe(true);
    expect(matches(ev({ status: 200 }), f)).toBe(false);
  });
});

describe('WINDOW_MS + windowCutoffMs', () => {
  it('maps each preset to its duration (all = null = unbounded)', () => {
    expect(WINDOW_MS['1m']).toBe(60_000);
    expect(WINDOW_MS['5m']).toBe(300_000);
    expect(WINDOW_MS['15m']).toBe(900_000);
    expect(WINDOW_MS['all']).toBeNull();
  });

  it('subtracts the window from now for a bounded preset', () => {
    expect(windowCutoffMs(1_000_000, '1m')).toBe(940_000);
    expect(windowCutoffMs(1_000_000, '5m')).toBe(700_000);
  });

  it('returns null (no cutoff) for the "all" preset', () => {
    expect(windowCutoffMs(1_000_000, 'all')).toBeNull();
  });
});

describe('sliceByWindow', () => {
  const events = [ev({ ts_ms: 30_000 }), ev({ ts_ms: 50_000 }), ev({ ts_ms: 100_000 })];

  it('drops events older than the cutoff (now - windowMs)', () => {
    // now=100_000, 1m window → cutoff 40_000; the 30_000 event is dropped.
    const kept = sliceByWindow(events, 100_000, '1m');
    expect(kept.map((e) => e.ts_ms)).toEqual([50_000, 100_000]);
  });

  it('keeps every event for the "all" window', () => {
    const kept = sliceByWindow(events, 100_000, 'all');
    expect(kept.map((e) => e.ts_ms)).toEqual([30_000, 50_000, 100_000]);
  });
});

describe('drilldownBuckets', () => {
  it('recomputes per-bucket count + nearest-rank p50/p95/max client-side (D-07)', () => {
    // Bucket 1 (ts 1000–1009 → key 1): latencies 1..10 → p50=5, p95=10, max=10.
    const bucket1 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((ms, i) =>
      ev({ ts_ms: 1000 + i, latency_ms: ms }),
    );
    // Bucket 2 (ts 2000, 2500 → key 2): latencies [20, 40] → p50=20, p95=40, max=40.
    const bucket2 = [ev({ ts_ms: 2500, latency_ms: 40 }), ev({ ts_ms: 2000, latency_ms: 20 })];

    // Feed the buckets deliberately out of order to prove ascending sort.
    const buckets = drilldownBuckets([...bucket2, ...bucket1]);

    expect(buckets).toEqual([
      { ts_ms: 1000, count: 10, p50_ms: 5, p95_ms: 10, max_ms: 10 },
      { ts_ms: 2000, count: 2, p50_ms: 20, p95_ms: 40, max_ms: 40 },
    ]);
  });

  it('returns an empty array for no events (empty groups omitted)', () => {
    expect(drilldownBuckets([])).toEqual([]);
  });

  it('honours a custom bucket width', () => {
    const buckets = drilldownBuckets(
      [ev({ ts_ms: 0, latency_ms: 3 }), ev({ ts_ms: 4000, latency_ms: 7 })],
      5000,
    );
    // Both fall in key 0 (floor(4000/5000)=0) → one bucket at ts 0.
    expect(buckets).toEqual([{ ts_ms: 0, count: 2, p50_ms: 3, p95_ms: 7, max_ms: 7 }]);
  });
});
