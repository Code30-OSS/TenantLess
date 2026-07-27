import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import CountBars from './CountBars';

/**
 * CONS-02 — the hand-rolled SVG horizontal bar chart (by route / by status). Pins the load-bearing
 * contracts: bar widths driven by `linScale` (largest count = full plot width), count-desc sort,
 * token-only colors (route `--text-2`, status via STATUS_TOKEN, selected `--gold`), and the
 * click-to-drill toggle (`onSelect(key)` / `onSelect(null)` on the already-selected bar).
 */

const noop = () => {};

describe('CountBars — bar geometry + sort', () => {
  it('scales bar widths with linScale so the largest count fills the plot width', () => {
    const { container } = render(
      <CountBars
        variant="route"
        counts={{ '/a': 100, '/b': 50, '/c': 25 }}
        selected={null}
        onSelect={noop}
        label="by route · session"
      />,
    );
    const widthFor = (key: string) =>
      (container.querySelector(`rect[data-key="${key}"]`) as SVGRectElement).getAttribute('width');
    // largest count → full plot width (100); the rest scale linearly.
    expect(widthFor('/a')).toBe('100');
    expect(widthFor('/b')).toBe('50');
    expect(widthFor('/c')).toBe('25');
  });

  it('sorts rows by count descending', () => {
    const { container } = render(
      <CountBars
        variant="route"
        counts={{ low: 3, high: 90, mid: 40 }}
        selected={null}
        onSelect={noop}
        label="by route · session"
      />,
    );
    const order = [...container.querySelectorAll('button[data-key]')].map((b) =>
      b.getAttribute('data-key'),
    );
    expect(order).toEqual(['high', 'mid', 'low']);
  });
});

describe('CountBars — colors', () => {
  it('gives the selected route bar the gold token', () => {
    const { container } = render(
      <CountBars
        variant="route"
        counts={{ '/a': 100, '/b': 50 }}
        selected="/a"
        onSelect={noop}
        label="by route · session"
      />,
    );
    const selected = container.querySelector('rect[data-key="/a"]') as SVGRectElement;
    const other = container.querySelector('rect[data-key="/b"]') as SVGRectElement;
    expect(selected.getAttribute('fill')).toBe('var(--gold)');
    expect(other.getAttribute('fill')).toBe('var(--text-2)');
  });

  it('colors status bars from STATUS_TOKEN (2xx green / 4xx amber / 5xx red)', () => {
    const { container } = render(
      <CountBars
        variant="status"
        counts={{ '200': 100, '404': 40, '503': 10 }}
        selected={null}
        onSelect={noop}
        label="by status · session"
      />,
    );
    const fill = (key: string) =>
      (container.querySelector(`rect[data-key="${key}"]`) as SVGRectElement).getAttribute('fill');
    expect(fill('200')).toBe('var(--green)');
    expect(fill('404')).toBe('var(--amber)');
    expect(fill('503')).toBe('var(--red)');
  });
});

describe('CountBars — click-to-drill', () => {
  it('fires onSelect(key) when an unselected bar is clicked', () => {
    const onSelect = vi.fn();
    const { container } = render(
      <CountBars
        variant="route"
        counts={{ '/a': 100, '/b': 50 }}
        selected={null}
        onSelect={onSelect}
        label="by route · session"
      />,
    );
    (container.querySelector('button[data-key="/b"]') as HTMLButtonElement).click();
    expect(onSelect).toHaveBeenCalledWith('/b');
  });

  it('fires onSelect(null) when the already-selected bar is clicked (toggle-clear)', () => {
    const onSelect = vi.fn();
    const { container } = render(
      <CountBars
        variant="route"
        counts={{ '/a': 100, '/b': 50 }}
        selected="/a"
        onSelect={onSelect}
        label="by route · session"
      />,
    );
    (container.querySelector('button[data-key="/a"]') as HTMLButtonElement).click();
    expect(onSelect).toHaveBeenCalledWith(null);
  });
});

describe('CountBars — empty states', () => {
  it('shows the no-requests copy when there are no counts and no selection', () => {
    render(
      <CountBars
        variant="route"
        counts={{}}
        selected={null}
        onSelect={noop}
        label="by route · session"
      />,
    );
    expect(screen.getByText('No requests recorded yet.')).toBeTruthy();
  });

  it('shows the filtered-empty copy + Clear when a selection excludes everything', () => {
    const onSelect = vi.fn();
    render(
      <CountBars
        variant="route"
        counts={{}}
        selected="/gone"
        onSelect={onSelect}
        label="by route · last 5m"
      />,
    );
    expect(screen.getByText('No requests match this filter.')).toBeTruthy();
    screen.getByRole('button', { name: 'Clear filter' }).click();
    expect(onSelect).toHaveBeenCalledWith(null);
  });

  it('renders the block sub-label verbatim', () => {
    render(
      <CountBars
        variant="status"
        counts={{ '200': 5 }}
        selected={null}
        onSelect={noop}
        label="by status · last 15m"
      />,
    );
    expect(screen.getByText('by status · last 15m')).toBeTruthy();
  });
});
