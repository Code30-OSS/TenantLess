import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import type { Summary } from '../api/types';

/**
 * Topbar live-metadata + manual-Refresh contract (UAT Gaps 4 + 5, WEBUI-04).
 *
 * The topbar must read seed / profile / tenantId from the shared `['summary']` cache (useSummary),
 * derive its status pill from the query state (loading → connecting, error → error, else running),
 * show `window.location.origin` for the mock URL, and expose a Refresh control that calls
 * `useQueryClient().invalidateQueries()` — the D-04-mandated manual re-fetch path (no polling).
 *
 * `../api/queries` (useSummary) and `@tanstack/react-query` (useQueryClient) are mocked so each
 * query state + the invalidate side-effect are driven directly, without a network or a provider.
 */

const { useSummaryMock, useQueryClientMock } = vi.hoisted(() => ({
  useSummaryMock: vi.fn(),
  useQueryClientMock: vi.fn(),
}));

vi.mock('../api/queries', () => ({
  useSummary: useSummaryMock,
}));

vi.mock('@tanstack/react-query', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@tanstack/react-query')>();
  return { ...actual, useQueryClient: useQueryClientMock };
});

import Topbar from './Topbar';

const summaryFixture: Summary = {
  tenantId: 'aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee',
  seed: 7,
  profile: 'data-dev',
  totals: { subscriptions: 0, resourceGroups: 0, resources: 0, violations: 0, dependencies: 0 },
  subscriptions: [],
  byType: [],
  byLocation: [],
};

interface SummaryState {
  data?: Summary;
  isLoading?: boolean;
  isError?: boolean;
}

function mockSummary(state: SummaryState) {
  useSummaryMock.mockReturnValue({
    data: undefined,
    isLoading: false,
    isError: false,
    ...state,
  });
}

let invalidateSpy: ReturnType<typeof vi.fn>;

function renderTopbar() {
  return render(<Topbar theme="dark" onToggleTheme={vi.fn()} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  invalidateSpy = vi.fn();
  useQueryClientMock.mockReturnValue({ invalidateQueries: invalidateSpy });
});

describe('Topbar — live tenant metadata (UAT Gap 4)', () => {
  it('renders seed / profile from useSummary, not the hardcoded 42 / enterprise-eu', () => {
    mockSummary({ data: summaryFixture });
    renderTopbar();

    expect(screen.getByText('7')).toBeTruthy();
    expect(screen.getByText('data-dev')).toBeTruthy();
    expect(screen.queryByText('42')).toBeNull();
    expect(screen.queryByText('enterprise-eu')).toBeNull();
    expect(screen.queryByText('8f3c1a90…4f31')).toBeNull();
  });

  it('does not crash while the summary is still loading (undefined-guarded meta)', () => {
    mockSummary({ isLoading: true });
    expect(() => renderTopbar()).not.toThrow();
    expect(screen.queryByText('42')).toBeNull();
  });
});

describe('Topbar — query-state status pill (UAT Gap 4)', () => {
  it('shows "connecting" while loading', () => {
    mockSummary({ isLoading: true });
    renderTopbar();
    expect(screen.getByText('connecting')).toBeTruthy();
  });

  it('shows "error" when the summary query failed', () => {
    mockSummary({ isError: true });
    renderTopbar();
    expect(screen.getByText('error')).toBeTruthy();
  });

  it('shows "running" on success', () => {
    mockSummary({ data: summaryFixture });
    renderTopbar();
    expect(screen.getByText('running')).toBeTruthy();
    expect(screen.queryByText('connecting')).toBeNull();
    expect(screen.queryByText('error')).toBeNull();
  });
});

describe('Topbar — real origin (UAT Gap 4)', () => {
  it('renders window.location.origin, not the hardcoded https://127.0.0.1:8443', () => {
    mockSummary({ data: summaryFixture });
    renderTopbar();
    expect(screen.getByText(window.location.origin)).toBeTruthy();
    expect(screen.queryByText('https://127.0.0.1:8443')).toBeNull();
  });
});

describe('Topbar — manual Refresh (UAT Gap 5, D-04)', () => {
  it('invalidates the query cache when Refresh is clicked', () => {
    mockSummary({ data: summaryFixture });
    renderTopbar();

    const refresh = screen.getByRole('button', { name: /refresh/i });
    expect(refresh).toBeTruthy();
    fireEvent.click(refresh);
    expect(invalidateSpy).toHaveBeenCalled();
  });
});
