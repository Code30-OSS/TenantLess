import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router';

import type {
  ArmListEnvelope,
  ArmResourceGroup,
  ArmResourceSummary,
  Summary,
  SubscriptionListResponse,
} from '../api/types';

/**
 * EXPL-GAP-02 — the ResourcesView 3-pane MILLER-COLUMN layout: col1 SubscriptionColumn |
 * col2 ResourceColumn | col3 ResourceDetail, driven by `?sub&rg&res`. Pins:
 *  - the KPI header renders the three `totals`
 *  - all three panes compose (col1/col2 headers + col3 detail)
 *  - selecting an RG in col1 sets `?sub&rg` and CLEARS `?res` (col2 reloads, col3 empties)
 *  - selecting a resource in col2 sets `?res` and feeds the detail hook
 *  - a search-result select deep-links exactly like a col2 selection (sub+rg+res)
 *  - a deep-link `?sub&rg&res` restores all three panes on mount
 *  - the CSS is a real three-track grid that collapses to a stacked column below 900px (tokens-only)
 */

const {
  useSummaryMock,
  useSubscriptionsMock,
  useResourceGroupsMock,
  useResourcesMock,
  useResourceDetailMock,
  useViolationsMock,
  useResourceSearchMock,
} = vi.hoisted(() => ({
  useSummaryMock: vi.fn(),
  useSubscriptionsMock: vi.fn(),
  useResourceGroupsMock: vi.fn(),
  useResourcesMock: vi.fn(),
  useResourceDetailMock: vi.fn(),
  useViolationsMock: vi.fn(),
  useResourceSearchMock: vi.fn(),
}));

vi.mock('../api/queries', () => ({
  useSummary: useSummaryMock,
  useSubscriptions: useSubscriptionsMock,
  useResourceGroups: useResourceGroupsMock,
  useResources: useResourcesMock,
  useResourceDetail: useResourceDetailMock,
  useViolations: useViolationsMock,
  useResourceSearch: useResourceSearchMock,
}));

import ResourcesView from './ResourcesView';

const ARM_ID =
  '/subscriptions/sub-a/resourceGroups/rg-app/providers/Microsoft.Storage/storageAccounts/stapp01';

const summaryFixture: Summary = {
  tenantId: '8f3c1a90-4f31',
  seed: 42,
  profile: 'enterprise-eu',
  totals: { subscriptions: 2, resourceGroups: 1284, resources: 102418, violations: 41, dependencies: 5 },
  subscriptions: [
    {
      subscriptionId: 'sub-a',
      name: 'sub-payments-prod',
      archetype: 'workload-prod',
      resourceCount: 9610,
      resourceGroupCount: 4,
      violationCount: 41,
    },
    {
      subscriptionId: 'sub-b',
      name: 'sub-data-dev',
      archetype: 'data-dev',
      resourceCount: 120,
      resourceGroupCount: 2,
      violationCount: 0,
    },
  ],
  byType: [],
  byLocation: [],
};

function ok<T>(data: T) {
  return { data, isLoading: false, isError: false, refetch: vi.fn() };
}

const rgs = (...names: string[]): ArmListEnvelope<ArmResourceGroup> => ({
  value: names.map((name) => ({ id: `/subscriptions/sub-a/resourceGroups/${name}`, name, location: 'westeurope' })),
});

const resources = (): ArmListEnvelope<ArmResourceSummary> => ({
  value: [{ id: ARM_ID, name: 'stapp01', type: 'Microsoft.Storage/storageAccounts', location: 'westeurope' }],
});

beforeEach(() => {
  useSummaryMock.mockReset().mockReturnValue(ok(summaryFixture));
  useSubscriptionsMock
    .mockReset()
    .mockReturnValue(ok<SubscriptionListResponse>({ count: 2, value: summaryFixture.subscriptions }));
  useResourceGroupsMock.mockReset().mockReturnValue(ok<ArmListEnvelope<ArmResourceGroup>>({ value: [] }));
  useResourcesMock.mockReset().mockReturnValue(ok<ArmListEnvelope<ArmResourceSummary>>({ value: [] }));
  useResourceDetailMock.mockReset().mockReturnValue(ok(undefined));
  useViolationsMock.mockReset().mockReturnValue(ok({ count: 0, value: [] }));
  useResourceSearchMock.mockReset().mockReturnValue(ok({ count: 0, value: [] }));
});

function renderView(path = '/ui/explorer/resources') {
  function LocationProbe() {
    const loc = useLocation();
    return <div data-testid="loc-search">{loc.search}</div>;
  }
  return render(
    <MemoryRouter basename="/ui" initialEntries={[path]}>
      <ResourcesView />
      <LocationProbe />
    </MemoryRouter>,
  );
}

describe('ResourcesView — KPI header', () => {
  it('renders the three totals from useSummary (localized) with their labels', () => {
    renderView();
    expect(screen.getByText('102,418')).toBeTruthy();
    expect(screen.getByText('1,284')).toBeTruthy();
    expect(screen.getByText('resources')).toBeTruthy();
    expect(screen.getByText('resource groups')).toBeTruthy();
    expect(screen.getByText('subscriptions')).toBeTruthy();
  });
});

describe('ResourcesView — 3-pane Miller composition', () => {
  it('renders all three panes: col1 Subscriptions, col2 Resources, col3 detail', () => {
    renderView();
    // column headers (capitalized) — distinct from the lowercase KPI labels
    expect(screen.getByText('Subscriptions')).toBeTruthy();
    expect(screen.getByText('Resources')).toBeTruthy();
    // col2 prompt (no RG selected) + col3 empty state
    expect(screen.getByText('Select a resource group to list its resources.')).toBeTruthy();
    expect(screen.getByText('Select a resource to inspect its ARM properties.')).toBeTruthy();
  });
});

describe('ResourcesView — col1 RG select fills col2 and clears col3', () => {
  it('onSelectRg sets ?sub&rg and DELETES ?res', () => {
    useResourceGroupsMock.mockReturnValue(ok(rgs('rg-app')));
    // start with a resource already selected (res present) under a different RG
    renderView(`/ui/explorer/resources?sub=sub-a&rg=rg-old&res=${encodeURIComponent(ARM_ID)}`);

    // sub-a is auto-expanded (deep-link) so its RG rows are visible; click the RG in col1.
    const tree = screen.getByLabelText('Subscriptions');
    fireEvent.click(within(tree).getByRole('button', { name: /rg-app/ }));

    const search = screen.getByTestId('loc-search').textContent ?? '';
    expect(search).toContain('sub=sub-a');
    expect(search).toContain('rg=rg-app');
    // col3 cleared — no resource selected after an RG change
    expect(search).not.toContain('res=');
  });
});

describe('ResourcesView — col2 resource select fills col3', () => {
  it('a col2 resource click sets ?res and feeds useResourceDetail', () => {
    useResourceGroupsMock.mockReturnValue(ok(rgs('rg-app')));
    useResourcesMock.mockReturnValue(ok(resources()));
    renderView('/ui/explorer/resources?sub=sub-a&rg=rg-app');

    // col2 lists the RG resources; click one (scope to the col2 "Resources" list).
    const col2 = screen.getByLabelText('Resources');
    fireEvent.click(within(col2).getByRole('button', { name: /stapp01/ }));

    expect(useResourceDetailMock).toHaveBeenCalledWith(ARM_ID);
    const search = screen.getByTestId('loc-search').textContent ?? '';
    expect(search).toContain('sub=sub-a');
    expect(search).toContain('rg=rg-app');
    expect(search).toContain(`res=${encodeURIComponent(ARM_ID)}`);
  });
});

describe('ResourcesView — search select deep-links like a col2 selection', () => {
  it('a committed search + result click updates ?sub&rg&res and feeds useResourceDetail', () => {
    useResourceSearchMock.mockReturnValue(
      ok({
        count: 1,
        value: [
          {
            id: ARM_ID,
            name: 'stapp01',
            type: 'Microsoft.Storage/storageAccounts',
            subscriptionId: 'sub-a',
            resourceGroupName: 'rg-app',
          },
        ],
      }),
    );
    renderView();

    const input = screen.getByLabelText('Search resources');
    fireEvent.change(input, { target: { value: 'stapp' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    const searchPanel = screen.getByLabelText('Resource search');
    fireEvent.click(within(searchPanel).getByRole('button', { name: /stapp01/ }));

    expect(useResourceDetailMock).toHaveBeenCalledWith(ARM_ID);
    const search = screen.getByTestId('loc-search').textContent ?? '';
    expect(search).toContain('sub=sub-a');
    expect(search).toContain('rg=rg-app');
    expect(search).toContain(`res=${encodeURIComponent(ARM_ID)}`);
  });
});

describe('ResourcesView — search Subscriptions select deep-links into Miller col1', () => {
  it('selecting a subscription match sets ?sub, DELETES ?rg + ?res, AND expands col1 in-session (WR-01)', () => {
    useResourceSearchMock.mockReturnValue(
      ok({
        count: 0,
        value: [],
        subscriptions: [{ id: 'sub-b', name: 'sub-data-dev' }],
      }),
    );
    // sub-b's RG list must be available so an in-session expand reveals real RG rows.
    useResourceGroupsMock.mockReturnValue(ok(rgs('rg-b-app')));
    // start under a DIFFERENT sub (sub-a) with an RG + resource already selected — the Explorer is
    // ALREADY mounted, so sub-b's SubRow exists but was seeded closed by the once-only useState.
    renderView(`/ui/explorer/resources?sub=sub-a&rg=rg-old&res=${encodeURIComponent(ARM_ID)}`);

    const tree = screen.getByLabelText('Subscriptions');
    // Precondition: sub-b (sub-data-dev) is collapsed before the in-session select (only sub-a is
    // auto-expanded from the deep-link mount → exactly one RG list is rendered in col1).
    const subBRow = within(tree).getByRole('button', { name: /sub-data-dev/ });
    expect(subBRow.getAttribute('aria-expanded')).toBe('false');
    expect(within(tree).getAllByRole('button', { name: /rg-b-app/ })).toHaveLength(1);

    const input = screen.getByLabelText('Search resources');
    fireEvent.change(input, { target: { value: 'dev' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    // click the subscription match INSIDE the search panel (not the col1 tree row)
    const searchPanel = screen.getByLabelText('Resource search');
    fireEvent.click(within(searchPanel).getByRole('button', { name: /sub-data-dev/ }));

    const search = screen.getByTestId('loc-search').textContent ?? '';
    expect(search).toContain('sub=sub-b');
    expect(search).not.toContain('rg=');
    expect(search).not.toContain('res=');

    // WR-01: col1 must actually reflect the in-session selection — sub-b is now expanded AND marked
    // selected (not merely a URL change). This FAILS against the once-only `useState(defaultOpen)`.
    const subBRowAfter = within(tree).getByRole('button', { name: /sub-data-dev/ });
    expect(subBRowAfter.getAttribute('aria-expanded')).toBe('true');
    expect(subBRowAfter.getAttribute('data-selected')).toBe('true');
    // sub-b's RG list is now ALSO rendered inline in col1 (a second rg-b-app appears alongside
    // sub-a's), proving the in-session expand actually mounted sub-b's children — not just a URL flip.
    expect(within(tree).getAllByRole('button', { name: /rg-b-app/ })).toHaveLength(2);
  });

  it('a ?sub-only deep link restores col1 with that subscription auto-expanded (initialSub)', () => {
    useResourceGroupsMock.mockReturnValue(ok(rgs('rg-app')));
    renderView('/ui/explorer/resources?sub=sub-a');

    // col1: sub-a auto-expanded via initialSub → its RG rows are visible; col2/col3 are empty states
    const tree = screen.getByLabelText('Subscriptions');
    expect(within(tree).getByRole('button', { name: /rg-app/ })).toBeTruthy();
    expect(screen.getByText('Select a resource group to list its resources.')).toBeTruthy();
    expect(screen.getByText('Select a resource to inspect its ARM properties.')).toBeTruthy();
  });
});

describe('ResourcesView — deep-link restore of all three panes', () => {
  it('restores col1 expansion + RG selection, col2 resource, and col3 detail on mount', () => {
    useResourceGroupsMock.mockReturnValue(ok(rgs('rg-app')));
    useResourcesMock.mockReturnValue(ok(resources()));
    renderView(`/ui/explorer/resources?sub=sub-a&rg=rg-app&res=${encodeURIComponent(ARM_ID)}`);

    // col1: sub-a auto-expanded → rg-app row visible and marked selected
    const tree = screen.getByLabelText('Subscriptions');
    const rgBtn = within(tree).getByRole('button', { name: /rg-app/ });
    expect(rgBtn.getAttribute('data-selected')).toBe('true');

    // col2: the selected RG's resources listed
    const col2 = screen.getByLabelText('Resources');
    expect(within(col2).getByRole('button', { name: /stapp01/ })).toBeTruthy();

    // col3: detail hook fed + breadcrumb echoes the resource
    expect(useResourceDetailMock).toHaveBeenCalledWith(ARM_ID);
    const crumb = screen.getByLabelText('Breadcrumb');
    expect(within(crumb).getByText('stapp01')).toBeTruthy();
  });
});

describe('ResourcesView.module.css — 3-column grid + stacked collapse (tokens-only)', () => {
  // Read the CSS module as text from disk (the vitest root is the `frontend/` package dir) so the
  // grid template + responsive collapse can be asserted structurally, not just as opaque class names.
  const css = readFileSync(resolve('src/explorer/ResourcesView.module.css'), 'utf8');

  it('.body is a THREE-track grid (col1 | col2 | 1fr)', () => {
    expect(css).toMatch(/grid-template-columns:\s*minmax\([^)]*\)\s+minmax\([^)]*\)\s+1fr/);
  });

  it('collapses to a single stacked column below the 900px breakpoint', () => {
    expect(css).toMatch(/@media\s*\(max-width:\s*900px\)[\s\S]*grid-template-columns:\s*1fr/);
  });

  it('each column scrolls independently (per-column max-height + overflow)', () => {
    expect(css).toMatch(/overflow-y:\s*auto/);
    expect(css).toMatch(/max-height:/);
  });

  it('carries no raw hex (tokens only, D-01)', () => {
    expect(css).not.toMatch(/#[0-9a-fA-F]{3,6}/);
  });
});
