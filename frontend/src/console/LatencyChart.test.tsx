import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import type { HistoryBucket, HistorySnapshot } from '../api/console';
import LatencyChart from './LatencyChart';

/**
 * CONS-01 — the hand-rolled SVG p50/p95/max latency chart. Pins the load-bearing contracts: one
 * `<polyline>` run per series when populated, a HONEST idle-gap break (interior null → 2 runs for that
 * series, Pitfall 5), the four-state ladder (loading / empty / error+Retry / populated), token-only
 * colors (no raw hex), and the locked `viewBox 0 0 720 180` geometry.
 */

function bucket(partial: Partial<HistoryBucket>): HistoryBucket {
  return {
    ts_ms: partial.ts_ms ?? 0,
    count: partial.count ?? 1,
    p50_ms: partial.p50_ms ?? 4,
    p95_ms: partial.p95_ms ?? 18,
    max_ms: partial.max_ms ?? 31,
  };
}

function snapshot(buckets: HistoryBucket[]): HistorySnapshot {
  return {
    bucket_ms: 1000,
    window_ms: 300_000,
    server_now_ms: buckets.length ? buckets[buckets.length - 1].ts_ms + 1000 : 0,
    buckets,
  };
}

const baseProps = {
  isLoading: false,
  isError: false,
  onRetry: vi.fn(),
  windowMs: 300_000,
  mode: 'aggregate' as const,
};

describe('LatencyChart — populated series', () => {
  it('renders exactly one <polyline> per series (p50/p95/max) with token-only strokes', () => {
    const history = snapshot([
      bucket({ ts_ms: 0, p50_ms: 3, p95_ms: 12, max_ms: 20 }),
      bucket({ ts_ms: 1000, p50_ms: 5, p95_ms: 18, max_ms: 31 }),
      bucket({ ts_ms: 2000, p50_ms: 4, p95_ms: 15, max_ms: 25 }),
    ]);
    const { container } = render(<LatencyChart {...baseProps} history={history} />);

    const polylines = container.querySelectorAll('polyline');
    expect(polylines).toHaveLength(3);
    const strokes = [...polylines].map((p) => p.getAttribute('stroke'));
    expect(strokes).toEqual(['var(--green)', 'var(--amber)', 'var(--red)']);
  });

  it('locks the viewBox to 0 0 720 180', () => {
    const { container } = render(
      <LatencyChart {...baseProps} history={snapshot([bucket({ ts_ms: 0 })])} />,
    );
    expect(container.querySelector('svg')?.getAttribute('viewBox')).toBe('0 0 720 180');
  });

  it('breaks a series with an interior null into TWO <polyline> runs (honest idle gap, Pitfall 5)', () => {
    const history = snapshot([
      bucket({ ts_ms: 0, p50_ms: 3, p95_ms: 12, max_ms: 20 }),
      // idle bucket — no traffic, null percentiles (must break the line, not draw to zero)
      { ts_ms: 1000, count: 0, p50_ms: null, p95_ms: null, max_ms: null },
      bucket({ ts_ms: 2000, p50_ms: 4, p95_ms: 15, max_ms: 25 }),
    ]);
    const { container } = render(<LatencyChart {...baseProps} history={history} />);

    // Each of the three series now has two runs → 6 polylines total.
    expect(container.querySelectorAll('polyline')).toHaveLength(6);
    expect(container.querySelectorAll('polyline[data-series="p50"]')).toHaveLength(2);
  });
});

describe('LatencyChart — state ladder', () => {
  it('renders the empty-window copy when the window has no traffic', () => {
    const history = snapshot([
      { ts_ms: 0, count: 0, p50_ms: null, p95_ms: null, max_ms: null },
      { ts_ms: 1000, count: 0, p50_ms: null, p95_ms: null, max_ms: null },
    ]);
    render(<LatencyChart {...baseProps} history={history} />);
    expect(
      screen.getByText('No requests in this window. Hit an ARM endpoint to watch latency react.'),
    ).toBeTruthy();
  });

  it('renders a loading skeleton while the first fetch is in flight', () => {
    const { container } = render(<LatencyChart {...baseProps} history={undefined} isLoading />);
    expect(container.querySelector('[aria-label="Loading latency history"]')).not.toBeNull();
    expect(container.querySelector('polyline')).toBeNull();
  });

  it('renders an inline error + Retry that calls onRetry', () => {
    const onRetry = vi.fn();
    render(<LatencyChart {...baseProps} history={undefined} isError onRetry={onRetry} />);
    expect(screen.getByText('Could not load latency history.')).toBeTruthy();
    screen.getByRole('button', { name: 'Retry' }).click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe('LatencyChart — mode label', () => {
  it('shows the aggregate advisory note only when a filter is active', () => {
    const history = snapshot([bucket({ ts_ms: 0 })]);
    const { rerender } = render(<LatencyChart {...baseProps} history={history} />);
    expect(screen.queryByText(/Aggregate series/)).toBeNull();

    rerender(<LatencyChart {...baseProps} history={history} filterActive />);
    expect(
      screen.getByText('Aggregate series — route/status filters apply to drill-down.'),
    ).toBeTruthy();
  });

  it('labels a drill-down series as client-computed', () => {
    render(
      <LatencyChart
        {...baseProps}
        history={undefined}
        mode="drilldown"
        drilldownSeries={[bucket({ ts_ms: 0 })]}
        drilldownLabel="Microsoft.Sql/servers"
      />,
    );
    expect(
      screen.getByText('drill-down · Microsoft.Sql/servers · client-computed'),
    ).toBeTruthy();
  });
});
