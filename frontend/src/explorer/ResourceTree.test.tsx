import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import type {
  ArmListEnvelope,
  ArmResourceGroup,
  ArmResourceSummary,
  SubscriptionListResponse,
  SummarySubscription,
} from '../api/types';

/**
 * EXPL-GAP-02 — the Miller-column halves of the Explorer:
 *  - col1 {@link SubscriptionColumn}: subscription rows → lazy RG list; an RG row is a SELECT button
 *    (it raises `onSelectRg`), NOT an inline expander — RGs never nest a resource list in col1.
 *  - col2 {@link ResourceColumn}: a muted prompt until `{sub,rg}` is set, then the selected RG's
 *    resources (lazy — `useResources` fires only when mounted); a resource row raises `onSelectResource`.
 *
 * Every data hook from `../api/queries` is mocked so the lazy-load / selection / pagination behaviors
 * are driven directly, without a network. Neither column uses react-router (the view lifts URL state).
 */

const { useSubscriptionsMock, useResourceGroupsMock, useResourcesMock } = vi.hoisted(() => ({
  useSubscriptionsMock: vi.fn(),
  useResourceGroupsMock: vi.fn(),
  useResourcesMock: vi.fn(),
}));

vi.mock('../api/queries', () => ({
  useSubscriptions: useSubscriptionsMock,
  useResourceGroups: useResourceGroupsMock,
  useResources: useResourcesMock,
}));

import {
  ResourceColumn,
  SubscriptionColumn,
  shortType,
  type RgSelection,
  type TreeSelection,
} from './ResourceTree';
import treeStyles from './ResourceTree.module.css';

const ARM_ID =
  '/subscriptions/sub-a/resourceGroups/rg-app/providers/Microsoft.Storage/storageAccounts/stapp01';

const subscriptions: SummarySubscription[] = [
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
];

const thirdSub: SummarySubscription = {
  subscriptionId: 'sub-c',
  name: 'sub-extra',
  archetype: 'sandbox',
  resourceCount: 7,
  resourceGroupCount: 1,
  violationCount: 0,
};

function ok<T>(data: T) {
  return { data, isLoading: false, isError: false, refetch: vi.fn() };
}

const rgs = (...names: string[]): ArmListEnvelope<ArmResourceGroup> => ({
  value: names.map((name) => ({
    id: `/subscriptions/sub-a/resourceGroups/${name}`,
    name,
    location: 'westeurope',
  })),
});

const resources = (): ArmListEnvelope<ArmResourceSummary> => ({
  value: [{ id: ARM_ID, name: 'stapp01', type: 'Microsoft.Storage/storageAccounts', location: 'westeurope' }],
});

beforeEach(() => {
  useSubscriptionsMock
    .mockReset()
    .mockReturnValue(ok<SubscriptionListResponse>({ count: 2, value: subscriptions }));
  useResourceGroupsMock.mockReset().mockReturnValue(ok<ArmListEnvelope<ArmResourceGroup>>({ value: [] }));
  useResourcesMock.mockReset().mockReturnValue(ok<ArmListEnvelope<ArmResourceSummary>>({ value: [] }));
});

function renderSubCol(partial: Partial<Parameters<typeof SubscriptionColumn>[0]> = {}) {
  const onSelectRg = vi.fn<(sel: RgSelection) => void>();
  const result = render(
    <SubscriptionColumn
      selectedSub={null}
      selectedRg={null}
      initialSub={null}
      initialRg={null}
      onSelectRg={onSelectRg}
      {...partial}
    />,
  );
  return { onSelectRg, container: result.container };
}

function renderResCol(partial: Partial<Parameters<typeof ResourceColumn>[0]> = {}) {
  const onSelectResource = vi.fn<(sel: TreeSelection) => void>();
  const result = render(
    <ResourceColumn
      sub={null}
      rg={null}
      selectedResId={null}
      onSelectResource={onSelectResource}
      {...partial}
    />,
  );
  return { onSelectResource, container: result.container };
}

describe('shortType', () => {
  it('returns the last ARM type segment', () => {
    expect(shortType('Microsoft.Storage/storageAccounts')).toBe('storageAccounts');
    expect(shortType('Microsoft.Sql/servers/databases')).toBe('databases');
  });
});

describe('SubscriptionColumn (col1) — subscription rows', () => {
  it('renders one row per subscription with name + archetype + resourceCount', () => {
    renderSubCol();
    expect(screen.getByText('sub-payments-prod')).toBeTruthy();
    expect(screen.getByText('workload-prod')).toBeTruthy();
    expect(screen.getByText('9610')).toBeTruthy();
    expect(screen.getByText('sub-data-dev')).toBeTruthy();
    expect(screen.getByText('data-dev')).toBeTruthy();
  });

  it('renders the per-subscription violation badge from its own /_sim/subscriptions row', () => {
    renderSubCol();
    expect(screen.getByText('41')).toBeTruthy();
  });

  it('renders the level inside a height-capped scroll region (page cannot grow unbounded)', () => {
    const { container } = renderSubCol();
    expect(container.querySelector(`.${treeStyles.scroll}`)).not.toBeNull();
  });

  it('paginates subscriptions with Prev/Next (replace, not append) + disabled edges', () => {
    const nextLink = '/_sim/subscriptions?%24top=100&%24skiptoken=SUBTOK2';
    useSubscriptionsMock.mockImplementation((params?: { skipToken?: string }) =>
      params?.skipToken
        ? ok<SubscriptionListResponse>({ count: 3, value: [thirdSub] })
        : ok<SubscriptionListResponse>({ count: 3, value: subscriptions, nextLink }),
    );
    renderSubCol();

    expect(screen.getByText('sub-payments-prod')).toBeTruthy();
    expect(screen.queryByText('sub-extra')).toBeNull();
    expect((screen.getByRole('button', { name: 'Previous subscriptions' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Next subscriptions' }) as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Next subscriptions' }));

    expect(useSubscriptionsMock).toHaveBeenCalledWith({ skipToken: 'SUBTOK2' });
    expect(screen.getByText('sub-extra')).toBeTruthy();
    expect(screen.queryByText('sub-payments-prod')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Previous subscriptions' }));
    expect(screen.getByText('sub-payments-prod')).toBeTruthy();
    expect(screen.queryByText('sub-extra')).toBeNull();
  });
});

describe('SubscriptionColumn (col1) — lazy RG list + RG select', () => {
  it('does not fetch resource groups until the subscription is expanded', () => {
    useResourceGroupsMock.mockReturnValue(ok(rgs('rg-app')));
    renderSubCol();
    expect(useResourceGroupsMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: /sub-payments-prod/ }));

    expect(useResourceGroupsMock).toHaveBeenCalledWith('sub-a', undefined);
    expect(screen.getByText('rg-app')).toBeTruthy();
  });

  it('raises onSelectRg({sub,rg}) when an RG row is clicked (RGs do NOT nest a resource list)', () => {
    useResourceGroupsMock.mockReturnValue(ok(rgs('rg-app')));
    const { onSelectRg } = renderSubCol();

    fireEvent.click(screen.getByRole('button', { name: /sub-payments-prod/ }));
    fireEvent.click(screen.getByRole('button', { name: /rg-app/ }));

    expect(onSelectRg).toHaveBeenCalledWith({ sub: 'sub-a', rg: 'rg-app' });
    // RGs never fetch a nested resource list inside col1.
    expect(useResourcesMock).not.toHaveBeenCalled();
  });

  it('marks the selected RG row (gold inset via data-selected)', () => {
    useResourceGroupsMock.mockReturnValue(ok(rgs('rg-app', 'rg-web')));
    renderSubCol({ selectedSub: 'sub-a', selectedRg: 'rg-app', initialSub: 'sub-a' });

    // sub-a auto-expanded (initialSub) so its RG rows are visible without a click.
    const selected = screen.getByRole('button', { name: /rg-app/ });
    const other = screen.getByRole('button', { name: /rg-web/ });
    expect(selected.getAttribute('data-selected')).toBe('true');
    expect(other.getAttribute('data-selected')).toBe('false');
  });

  it('paginates the RG list with Prev/Next, replacing the page', () => {
    const rgNext = '/subscriptions/sub-a/resourceGroups?%24skiptoken=RGTOK';
    useResourceGroupsMock.mockImplementation((_sub: string, params?: { skipToken?: string }) =>
      params?.skipToken
        ? ok<ArmListEnvelope<ArmResourceGroup>>({
            value: [{ id: '/subscriptions/sub-a/resourceGroups/rg-two', name: 'rg-two', location: 'westeurope' }],
          })
        : ok<ArmListEnvelope<ArmResourceGroup>>({
            value: [{ id: '/subscriptions/sub-a/resourceGroups/rg-one', name: 'rg-one', location: 'westeurope' }],
            nextLink: rgNext,
          }),
    );
    renderSubCol();

    fireEvent.click(screen.getByRole('button', { name: /sub-payments-prod/ }));
    expect(screen.getByText('rg-one')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Previous resource groups' }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: 'Next resource groups' }));
    expect(useResourceGroupsMock).toHaveBeenCalledWith('sub-a', { skipToken: 'RGTOK' });
    expect(screen.getByText('rg-two')).toBeTruthy();
    expect(screen.queryByText('rg-one')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Previous resource groups' }));
    expect(screen.getByText('rg-one')).toBeTruthy();
    expect(screen.queryByText('rg-two')).toBeNull();
  });

  it('shows the empty row when a subscription has zero resource groups', () => {
    useResourceGroupsMock.mockReturnValue(ok({ value: [] }));
    renderSubCol();
    fireEvent.click(screen.getByRole('button', { name: /sub-payments-prod/ }));
    expect(screen.getByText('No resource groups in this subscription.')).toBeTruthy();
  });
});

describe('ResourceColumn (col2) — prompt + lazy resources', () => {
  it('renders a muted prompt and fetches NO resources until an RG is selected', () => {
    renderResCol();
    expect(screen.getByText(/Select a resource group/i)).toBeTruthy();
    expect(useResourcesMock).not.toHaveBeenCalled();
  });

  it('lazily loads the selected RG resources for {sub,rg} and lists name + short type', () => {
    useResourcesMock.mockReturnValue(ok(resources()));
    renderResCol({ sub: 'sub-a', rg: 'rg-app' });

    // col2 always passes the params object ({ filter, skipToken }); no filter/cursor → both undefined.
    expect(useResourcesMock).toHaveBeenCalledWith('sub-a', 'rg-app', {});
    expect(screen.getByText('stapp01')).toBeTruthy();
    expect(screen.getByText('storageAccounts')).toBeTruthy();
  });

  it('raises onSelectResource({armId,sub,rg}) when a resource row is clicked', () => {
    useResourcesMock.mockReturnValue(ok(resources()));
    const { onSelectResource } = renderResCol({ sub: 'sub-a', rg: 'rg-app' });

    fireEvent.click(screen.getByRole('button', { name: /stapp01/ }));
    expect(onSelectResource).toHaveBeenCalledWith({ armId: ARM_ID, sub: 'sub-a', rg: 'rg-app' });
  });

  it('marks the selected resource row (gold inset via data-selected)', () => {
    useResourcesMock.mockReturnValue(ok(resources()));
    renderResCol({ sub: 'sub-a', rg: 'rg-app', selectedResId: ARM_ID });
    expect(screen.getByRole('button', { name: /stapp01/ }).getAttribute('data-selected')).toBe('true');
  });

  it('paginates resources with Prev/Next, replacing the page', () => {
    const resNext = '/subscriptions/sub-a/resourceGroups/rg-app/resources?%24skiptoken=RESTOK';
    const res2: ArmResourceSummary = {
      id: `${ARM_ID}-2`,
      name: 'stapp02',
      type: 'Microsoft.Storage/storageAccounts',
      location: 'westeurope',
    };
    useResourcesMock.mockImplementation((_sub: string, _rg: string, params?: { skipToken?: string }) =>
      params?.skipToken
        ? ok<ArmListEnvelope<ArmResourceSummary>>({ value: [res2] })
        : ok<ArmListEnvelope<ArmResourceSummary>>({ value: resources().value, nextLink: resNext }),
    );
    renderResCol({ sub: 'sub-a', rg: 'rg-app' });

    expect(screen.getByText('stapp01')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Next resources' }));
    expect(useResourcesMock).toHaveBeenCalledWith('sub-a', 'rg-app', { skipToken: 'RESTOK' });
    expect(screen.getByText('stapp02')).toBeTruthy();
    expect(screen.queryByText('stapp01')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Previous resources' }));
    expect(screen.getByText('stapp01')).toBeTruthy();
    expect(screen.queryByText('stapp02')).toBeNull();
  });

  it('shows the empty row when the selected RG has zero resources', () => {
    useResourcesMock.mockReturnValue(ok({ value: [] }));
    renderResCol({ sub: 'sub-a', rg: 'rg-app' });
    expect(screen.getByText('No resources in this group.')).toBeTruthy();
  });

  it('threads the applied $filter (EXPL-05) into the resource query — col2 is the single list surface', () => {
    useResourcesMock.mockReturnValue(ok(resources()));
    renderResCol({ sub: 'sub-a', rg: 'rg-app', filter: "resourceType eq 'Microsoft.Storage/storageAccounts'" });
    expect(useResourcesMock).toHaveBeenCalledWith('sub-a', 'rg-app', {
      filter: "resourceType eq 'Microsoft.Storage/storageAccounts'",
    });
  });

  it('surfaces the ARM fail-closed message on a bad $filter (Invalid filter: <msg>)', () => {
    useResourcesMock.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error('unterminated string literal'),
      refetch: vi.fn(),
    });
    renderResCol({ sub: 'sub-a', rg: 'rg-app', filter: "resourceType eq 'oops" });
    expect(screen.getByText('Invalid filter: unterminated string literal')).toBeTruthy();
  });

  it('shows a filter-specific empty row when nothing matches the applied $filter', () => {
    useResourcesMock.mockReturnValue(ok({ value: [] }));
    renderResCol({ sub: 'sub-a', rg: 'rg-app', filter: "location eq 'nowhere'" });
    expect(screen.getByText('No resources match this filter.')).toBeTruthy();
  });

  it('resets its pager when the selected RG changes (no stale cursor)', () => {
    useResourcesMock.mockReturnValue(ok(resources()));
    const { onSelectResource } = renderResCol({ sub: 'sub-a', rg: 'rg-app' });
    // re-render with a different RG — ResList remounts (keyed by sub/rg), so useResources is
    // re-invoked for the new RG with a fresh (undefined) cursor.
    render(
      <ResourceColumn sub="sub-a" rg="rg-web" selectedResId={null} onSelectResource={onSelectResource} />,
    );
    expect(useResourcesMock).toHaveBeenCalledWith('sub-a', 'rg-web', {});
  });
});
