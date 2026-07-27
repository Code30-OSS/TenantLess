import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

/**
 * ServerStatusView (D-09/D-16, 17-UI-SPEC §6) — a light, REUSE-ONLY server status view.
 *
 * `useSummary` (the SAME `/_sim/summary` the topbar reads) is mocked so the two states are asserted
 * without a network AND without a new backend endpoint (D-16, Assumption #5):
 *  - active tenant → the tenant meta + resource/subscription counts + a running pill + the mock URL
 *  - empty tenant (post-reset, tenantId null) → "No active tenant" + zero counts, never a crash (D-09)
 */

const { useSummaryMock } = vi.hoisted(() => ({ useSummaryMock: vi.fn() }));
vi.mock('../api/queries', () => ({ useSummary: useSummaryMock }));

import ServerStatusView from './ServerStatusView';

beforeEach(() => {
  useSummaryMock.mockReset();
});

describe('ServerStatusView — active tenant', () => {
  it('renders the running pill, tenant meta, and reused resource/subscription counts', () => {
    useSummaryMock.mockReturnValue({
      data: {
        tenantId: 't-abcd1234',
        seed: 42,
        profile: 'enterprise',
        totals: { subscriptions: 3, resourceGroups: 12, resources: 1000, violations: 5 },
      },
      isLoading: false,
      isError: false,
    });
    render(<ServerStatusView />);

    expect(screen.getByText('running')).toBeTruthy();
    expect(screen.queryByText('No active tenant')).toBeNull();
    // KpiStat thousands-groups the reused count.
    expect(screen.getByText('1,000')).toBeTruthy();
    // The gold mock URL comes from window.location (no new endpoint).
    expect(screen.getByText(/mock http/i)).toBeTruthy();
  });
});

describe('ServerStatusView — empty tenant (post-reset, D-09)', () => {
  it('shows "No active tenant" + zero counts without crashing', () => {
    useSummaryMock.mockReturnValue({
      data: {
        tenantId: '',
        seed: 0,
        profile: '',
        totals: { subscriptions: 0, resourceGroups: 0, resources: 0, violations: 0 },
      },
      isLoading: false,
      isError: false,
    });
    render(<ServerStatusView />);

    expect(screen.getByText('No active tenant')).toBeTruthy();
    expect(
      screen.getByText('Generate a tenant or restore a snapshot to start serving ARM.'),
    ).toBeTruthy();
  });
});
