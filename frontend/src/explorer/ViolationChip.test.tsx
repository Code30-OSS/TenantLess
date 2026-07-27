import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

import type { Summary, ViolationsResponse } from '../api/types';

/**
 * EXPL-03 — severity-colored governance chips + the isolated violations block that hosts them.
 * `useViolations` is mocked so the block's populated / empty / error states are driven directly.
 * Pins: severity→token color map (High→red, Medium→amber, Low→green), one chip per violation code,
 * a clean-resource empty state, and an isolated error+Retry that never blanks the rest of the panel.
 */

const { useViolationsMock } = vi.hoisted(() => ({ useViolationsMock: vi.fn() }));
vi.mock('../api/queries', () => ({ useViolations: useViolationsMock }));

import {
  ViolationChip,
  ViolationsBlock,
  SEVERITY_TOKEN,
  subscriptionViolationCount,
} from './ViolationChip';

beforeEach(() => {
  useViolationsMock.mockReset();
});

function violationsState(partial: {
  data?: ViolationsResponse;
  isLoading?: boolean;
  isError?: boolean;
  refetch?: () => void;
}) {
  return {
    data: partial.data,
    isLoading: partial.isLoading ?? false,
    isError: partial.isError ?? false,
    refetch: partial.refetch ?? vi.fn(),
  };
}

describe('ViolationChip — severity → color token', () => {
  it('maps High→--red, Medium→--amber, Low→--green', () => {
    expect(SEVERITY_TOKEN.High).toBe('--red');
    expect(SEVERITY_TOKEN.Medium).toBe('--amber');
    expect(SEVERITY_TOKEN.Low).toBe('--green');
  });

  it('applies the High severity color token to the chip', () => {
    const { container } = render(<ViolationChip severity="High" code="STORAGE_HTTPS_NOT_ENFORCED" />);
    const chip = container.querySelector('[data-severity="High"]') as HTMLElement;
    expect(chip).not.toBeNull();
    expect(chip.style.color).toBe('var(--red)');
    expect(chip.textContent).toContain('STORAGE_HTTPS_NOT_ENFORCED');
  });

  it('applies the Medium and Low color tokens', () => {
    const { container: medium } = render(<ViolationChip severity="Medium" code="TLS_OUTDATED" />);
    expect((medium.querySelector('[data-severity="Medium"]') as HTMLElement).style.color).toBe(
      'var(--amber)',
    );
    const { container: low } = render(<ViolationChip severity="Low" code="TAG_MISSING" />);
    expect((low.querySelector('[data-severity="Low"]') as HTMLElement).style.color).toBe(
      'var(--green)',
    );
  });
});

describe('ViolationsBlock — populated / empty / error', () => {
  it('renders one chip per violation code from useViolations({resource})', () => {
    useViolationsMock.mockReturnValue(
      violationsState({
        data: {
          count: 3,
          value: [
            { resourceId: '/r', code: 'PUBLIC_NETWORK_ACCESS_ENABLED', severity: 'High', subscriptionId: 's', detail: {} },
            { resourceId: '/r', code: 'TLS_VERSION_OUTDATED', severity: 'Medium', subscriptionId: 's', detail: {} },
            { resourceId: '/r', code: 'TAG_MISSING', severity: 'Low', subscriptionId: 's', detail: {} },
          ],
        },
      }),
    );
    const { container } = render(<ViolationsBlock resource="/subscriptions/b7e2/x" />);

    expect(container.querySelectorAll('[data-severity]')).toHaveLength(3);
    expect(screen.getByText('PUBLIC_NETWORK_ACCESS_ENABLED')).toBeTruthy();
    expect(screen.getByText('TLS_VERSION_OUTDATED')).toBeTruthy();
    expect(screen.getByText('TAG_MISSING')).toBeTruthy();
  });

  it('shows the clean-resource empty state (not an error) when there are zero violations', () => {
    useViolationsMock.mockReturnValue(violationsState({ data: { count: 0, value: [] } }));
    render(<ViolationsBlock resource="/subscriptions/b7e2/x" />);
    expect(screen.getByText('No governance violations.')).toBeTruthy();
  });

  it('shows an isolated "Could not load violations." + Retry on a fetch error', () => {
    const refetch = vi.fn();
    useViolationsMock.mockReturnValue(violationsState({ isError: true, refetch }));
    render(<ViolationsBlock resource="/subscriptions/b7e2/x" />);

    expect(screen.getByText('Could not load violations.')).toBeTruthy();
    const retry = screen.getByRole('button', { name: 'Retry' });
    retry.click();
    expect(refetch).toHaveBeenCalled();
  });
});

describe('subscriptionViolationCount — per-sub rollup helper for the tree (15-05)', () => {
  const summary = {
    subscriptions: [
      { subscriptionId: 's1', violationCount: 41 },
      { subscriptionId: 's2', violationCount: 0 },
    ],
  } as Summary;

  it('returns the per-subscription violationCount', () => {
    expect(subscriptionViolationCount(summary, 's1')).toBe(41);
    expect(subscriptionViolationCount(summary, 's2')).toBe(0);
  });

  it('returns 0 for an unknown subscription id', () => {
    expect(subscriptionViolationCount(summary, 'nope')).toBe(0);
  });
});
