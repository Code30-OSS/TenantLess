import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import ConsoleFilters from './ConsoleFilters';
import LiveStatusPill from './LiveStatusPill';
import type { ConsoleFilter, StatusClass } from './filter';

/**
 * CONS-04 — the INSTANT-APPLY filter bar + the live/paused pill. Pins the load-bearing contracts:
 * a status chip toggles `onChange` immediately (multi-select, no Enter/blur gating); a route chip
 * single-selects; a window chip sets `window`; `clear filters` resets to the default `{[], null, '5m'}`;
 * an active status chip carries its STATUS_TOKEN color; an active route/window chip carries `--gold`;
 * the pill shows `● LIVE` (blink dot) when live, `❚❚ PAUSED` when paused, and toggles on the button.
 */

function filter(overrides: Partial<ConsoleFilter> = {}): ConsoleFilter {
  return { status: new Set<StatusClass>(), route: null, window: '5m', ...overrides };
}

describe('ConsoleFilters — instant-apply status chips (multi-select)', () => {
  it('toggles a status class into the set immediately on click (no Enter needed)', () => {
    const onChange = vi.fn();
    render(<ConsoleFilters filter={filter()} routes={[]} onChange={onChange} />);

    screen.getByRole('button', { name: '4xx' }).click();

    expect(onChange).toHaveBeenCalledTimes(1);
    const next: ConsoleFilter = onChange.mock.calls[0][0];
    expect(next.status.has('4xx')).toBe(true);
    expect([...next.status]).toEqual(['4xx']);
  });

  it('removes an already-active status class (multi-select toggle off)', () => {
    const onChange = vi.fn();
    render(
      <ConsoleFilters
        filter={filter({ status: new Set<StatusClass>(['4xx', '5xx']) })}
        routes={[]}
        onChange={onChange}
      />,
    );

    screen.getByRole('button', { name: '4xx' }).click();

    const next: ConsoleFilter = onChange.mock.calls[0][0];
    expect(next.status.has('4xx')).toBe(false);
    expect(next.status.has('5xx')).toBe(true);
  });

  it('colors an active status chip with its STATUS_TOKEN (var, no raw hex)', () => {
    render(
      <ConsoleFilters
        filter={filter({ status: new Set<StatusClass>(['5xx']) })}
        routes={[]}
        onChange={() => {}}
      />,
    );
    const chip = screen.getByRole('button', { name: '5xx' });
    expect(chip.style.color).toBe('var(--red)');
    expect(chip.style.borderColor).toBe('var(--red)');
  });
});

describe('ConsoleFilters — route (single-select) + window presets', () => {
  it('single-selects a route on click', () => {
    const onChange = vi.fn();
    render(
      <ConsoleFilters
        filter={filter()}
        routes={['/subscriptions/{sub}', '/subscriptions/{sub}/resourceGroups']}
        onChange={onChange}
      />,
    );

    screen.getByRole('button', { name: '/subscriptions/{sub}' }).click();

    const next: ConsoleFilter = onChange.mock.calls[0][0];
    expect(next.route).toBe('/subscriptions/{sub}');
  });

  it('gives the active route chip the gold token color', () => {
    render(
      <ConsoleFilters
        filter={filter({ route: '/r/a' })}
        routes={['/r/a', '/r/b']}
        onChange={() => {}}
      />,
    );
    const active = screen.getByRole('button', { name: '/r/a' });
    // The active chip carries the gold class (assertable via computed style token).
    expect(active.className).toContain('chipGold');
  });

  it('selects a window preset (All → window "all")', () => {
    const onChange = vi.fn();
    render(<ConsoleFilters filter={filter()} routes={[]} onChange={onChange} />);

    screen.getByRole('button', { name: 'All' }).click();

    const next: ConsoleFilter = onChange.mock.calls[0][0];
    expect(next.window).toBe('all');
  });
});

describe('ConsoleFilters — clear filters', () => {
  it('resets to the default { status: [], route: null, window: "5m" }', () => {
    const onChange = vi.fn();
    render(
      <ConsoleFilters
        filter={filter({ status: new Set<StatusClass>(['4xx']), route: '/r/a', window: '15m' })}
        routes={['/r/a']}
        onChange={onChange}
      />,
    );

    screen.getByRole('button', { name: 'clear filters' }).click();

    const next: ConsoleFilter = onChange.mock.calls[0][0];
    expect([...next.status]).toEqual([]);
    expect(next.route).toBeNull();
    expect(next.window).toBe('5m');
  });

  it('hides clear filters when the filter is already at default', () => {
    render(<ConsoleFilters filter={filter()} routes={[]} onChange={() => {}} />);
    expect(screen.queryByRole('button', { name: 'clear filters' })).toBeNull();
  });
});

describe('LiveStatusPill', () => {
  it('renders ● LIVE with the blinking dot when live and not paused', () => {
    const { container } = render(
      <LiveStatusPill status="live" paused={false} onToggle={() => {}} />,
    );
    expect(screen.getByText('● LIVE')).toBeTruthy();
    // The dot carries the blink class (green, blinking).
    const blinkDot = container.querySelector('[class*="blink"]');
    expect(blinkDot).toBeTruthy();
    expect((blinkDot as HTMLElement).style.background).toBe('var(--green)');
    expect(screen.getByRole('button', { name: 'Pause' })).toBeTruthy();
  });

  it('renders ❚❚ PAUSED with a steady dot and a Resume button when paused', () => {
    const { container } = render(
      <LiveStatusPill status="live" paused={true} onToggle={() => {}} />,
    );
    expect(screen.getByText('❚❚ PAUSED')).toBeTruthy();
    expect(container.querySelector('[class*="blink"]')).toBeNull(); // no blink while paused
    expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy();
  });

  it('shows connecting… before the socket opens', () => {
    render(<LiveStatusPill status="connecting" paused={false} onToggle={() => {}} />);
    expect(screen.getByText('connecting…')).toBeTruthy();
  });

  it('calls onToggle when the Pause/Resume button is clicked', () => {
    const onToggle = vi.fn();
    render(<LiveStatusPill status="live" paused={false} onToggle={onToggle} />);
    screen.getByRole('button', { name: 'Pause' }).click();
    expect(onToggle).toHaveBeenCalledTimes(1);
  });
});
