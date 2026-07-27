import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';

import type { RequestEvent } from '../api/console';
import FeedTable from './FeedTable';

/**
 * CONS-04 — the live request feed. Pins the load-bearing contracts: rows render in the given
 * (newest-first) order, the status cell carries the STATUS_TOKEN color for its class, clicking a row
 * raises `onSelect` with that event, the selected row gets the gold inset accent, and the state ladder
 * (filtered-empty / live-empty / reconnecting) renders the exact UI-SPEC copy. Every cell is
 * auto-escaped JSX text (T-16-03) — asserted implicitly by the token-string (not innerHTML) rendering.
 */

function ev(over: Partial<RequestEvent>): RequestEvent {
  return {
    ts_ms: 1_000,
    method: 'GET',
    path: '/x',
    route: '/{p}',
    status: 200,
    latency_ms: 3,
    ...over,
  };
}

describe('FeedTable — rows', () => {
  const events = [
    ev({ ts_ms: 3_000, path: '/one', status: 200 }),
    ev({ ts_ms: 2_000, path: '/two', status: 404 }),
    ev({ ts_ms: 1_000, path: '/three', status: 503 }),
  ];

  it('renders rows in the given newest-first order', () => {
    const { container } = render(
      <FeedTable events={events} selected={null} onSelect={() => {}} state="live" emptyKind="no-traffic" />,
    );
    const paths = [...container.querySelectorAll('tbody tr [data-col="path"]')].map((c) => c.textContent);
    expect(paths).toEqual(['/one', '/two', '/three']);
  });

  it('tints the status cell with the STATUS_TOKEN color for 2xx/4xx/5xx', () => {
    const { container } = render(
      <FeedTable events={events} selected={null} onSelect={() => {}} state="live" emptyKind="no-traffic" />,
    );
    const statusCells = [...container.querySelectorAll('tbody tr [data-col="status"]')];
    expect(statusCells[0].getAttribute('style')).toContain('var(--green)');
    expect(statusCells[1].getAttribute('style')).toContain('var(--amber)');
    expect(statusCells[2].getAttribute('style')).toContain('var(--red)');
  });

  it('calls onSelect with the clicked row event', () => {
    const onSelect = vi.fn();
    const { container } = render(
      <FeedTable events={events} selected={null} onSelect={onSelect} state="live" emptyKind="no-traffic" />,
    );
    (container.querySelector('tbody tr') as HTMLElement).click();
    expect(onSelect).toHaveBeenCalledWith(events[0]);
  });

  it('gold-accents the selected row and leaves other rows unaccented', () => {
    const { container } = render(
      <FeedTable events={events} selected={events[1]} onSelect={() => {}} state="live" emptyKind="no-traffic" />,
    );
    const rows = [...container.querySelectorAll('tbody tr')] as HTMLElement[];
    expect(rows[1].getAttribute('style')).toContain('var(--gold)');
    expect(rows[0].getAttribute('style') ?? '').not.toContain('var(--gold)');
  });
});

describe('FeedTable — state ladder', () => {
  it('renders the filtered-empty copy for an empty filtered feed', () => {
    const { getByText } = render(
      <FeedTable events={[]} selected={null} onSelect={() => {}} state="live" emptyKind="filtered" />,
    );
    expect(getByText('No requests match this filter.')).toBeTruthy();
  });

  it('renders the waiting copy for a live empty (no-traffic) feed', () => {
    const { getByText } = render(
      <FeedTable events={[]} selected={null} onSelect={() => {}} state="live" emptyKind="no-traffic" />,
    );
    expect(
      getByText('Waiting for requests — the feed updates as the mock serves ARM traffic.'),
    ).toBeTruthy();
  });

  it('renders the reconnecting banner above retained rows', () => {
    const { getByText, container } = render(
      <FeedTable
        events={[ev({ path: '/kept' })]}
        selected={null}
        onSelect={() => {}}
        state="reconnecting"
        emptyKind="no-traffic"
      />,
    );
    expect(getByText('Live feed disconnected — reconnecting…')).toBeTruthy();
    expect(container.querySelector('tbody tr [data-col="path"]')?.textContent).toBe('/kept');
  });
});
