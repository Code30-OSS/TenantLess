import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/react';

import type { RequestEvent } from '../api/console';
import RequestInspector from './RequestInspector';

/**
 * CONS-03 / D-06 — the request inspector. Pins the load-bearing contracts: the empty-selection
 * placeholder, EXACTLY the six captured RequestEvent fields (no invented headers/query/bodies),
 * `Copy event` serializing those six fields via `navigator.clipboard.writeText`, and XSS-safety —
 * a scanner-injected `<img onerror>` in `path` renders as inert escaped text (T-16-03).
 */

const fixture: RequestEvent = {
  ts_ms: 1_700_000_000_000,
  method: 'GET',
  path: '/subscriptions/abc/resourceGroups/rg1',
  route: '/subscriptions/{s}/resourceGroups/{g}',
  status: 200,
  latency_ms: 7,
};

describe('RequestInspector — empty state', () => {
  it('renders the placeholder when nothing is selected', () => {
    const { getByText } = render(<RequestInspector selected={null} />);
    expect(getByText('Select a request from the feed to inspect it.')).toBeTruthy();
  });
});

describe('RequestInspector — populated (D-06 six fields)', () => {
  it('renders EXACTLY the six captured fields — no invented fields', () => {
    const { container } = render(<RequestInspector selected={fixture} />);
    const fields = [...container.querySelectorAll('[data-field]')].map((f) => f.getAttribute('data-field'));
    expect(fields).toEqual(['method', 'path', 'route', 'status', 'latency_ms', 'ts_ms']);
    expect(fields).toHaveLength(6);
  });

  it('copies the six-field JSON via navigator.clipboard.writeText', () => {
    const writeText = vi.fn();
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const { getByRole } = render(<RequestInspector selected={fixture} />);
    getByRole('button', { name: 'Copy event' }).click();
    expect(writeText).toHaveBeenCalledWith(
      JSON.stringify({
        method: fixture.method,
        path: fixture.path,
        route: fixture.route,
        status: fixture.status,
        latency_ms: fixture.latency_ms,
        ts_ms: fixture.ts_ms,
      }),
    );
  });
});

describe('RequestInspector — XSS safety (T-16-03)', () => {
  it('renders a malicious path as inert escaped text (no HTML injection)', () => {
    const malicious: RequestEvent = { ...fixture, path: '<img src=x onerror="alert(1)">' };
    const { container } = render(<RequestInspector selected={malicious} />);
    expect(container.querySelector('img')).toBeNull();
    const pathCell = container.querySelector('[data-field="path"]');
    expect(pathCell?.textContent).toContain('<img src=x onerror="alert(1)">');
  });
});
