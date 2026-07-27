import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';

import type { ResourceSearchResponse, ResourceSearchResult } from '../api/types';

/**
 * ResourceSearch (15-14, EXPL-01/EXPL-05) — the tenant-wide server-side search box + flat, bounded
 * paginated result list that deep-links a hit into the detail panel. `useResourceSearch` is mocked so
 * the draft/applied discipline, the Prev/Next replace paging, and the select wiring are driven directly.
 *
 * Pins:
 *  - draft/applied: typing fires NO request (only a COMMITTED term reaches the hook's `q`)
 *  - results render as a FLAT list (name + short type + subscription/RG hint)
 *  - Prev/Next REPLACE pages (bounded, same idiom as the tree), disabled at the edges
 *  - selecting a result row raises onSelectResource({ armId, sub, rg })
 */

const { useResourceSearchMock } = vi.hoisted(() => ({ useResourceSearchMock: vi.fn() }));

vi.mock('../api/queries', () => ({ useResourceSearch: useResourceSearchMock }));

import ResourceSearch from './ResourceSearch';

const ARM1 =
  '/subscriptions/sub-a/resourceGroups/rg-app/providers/Microsoft.Storage/storageAccounts/stapp01';
const ARM2 = '/subscriptions/sub-b/resourceGroups/rg-net/providers/Microsoft.Network/virtualNetworks/vnet-demo';

const row1: ResourceSearchResult = {
  id: ARM1,
  name: 'stapp01',
  type: 'Microsoft.Storage/storageAccounts',
  subscriptionId: 'sub-a',
  resourceGroupName: 'rg-app',
};
const row2: ResourceSearchResult = {
  id: ARM2,
  name: 'vnet-demo',
  type: 'Microsoft.Network/virtualNetworks',
  subscriptionId: 'sub-b',
  resourceGroupName: 'rg-net',
};

function ok(data: Partial<ResourceSearchResponse> & Pick<ResourceSearchResponse, 'count' | 'value'>) {
  // `subscriptions`/`resourceGroups` default to [] so existing fixtures need not restate them
  // (15-17 added subscriptions; RG-name search adds resourceGroups).
  return {
    data: { subscriptions: [], resourceGroups: [], ...data } as ResourceSearchResponse,
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  };
}

/** The last `params` object the mocked hook was called with (its query key inputs). */
function lastParams(): { q: string; subscription?: string; skipToken?: string } {
  const calls = useResourceSearchMock.mock.calls;
  return calls[calls.length - 1]?.[0] ?? { q: '' };
}

beforeEach(() => {
  useResourceSearchMock.mockReset().mockReturnValue(ok({ count: 0, value: [] }));
});

function renderSearch() {
  const onSelectResource = vi.fn();
  const onSelectSubscription = vi.fn();
  const onSelectResourceGroup = vi.fn();
  render(
    <ResourceSearch
      onSelectResource={onSelectResource}
      onSelectSubscription={onSelectSubscription}
      onSelectResourceGroup={onSelectResourceGroup}
      selectedResId={null}
    />,
  );
  return { onSelectResource, onSelectSubscription, onSelectResourceGroup };
}

function applyTerm(term: string) {
  const input = screen.getByLabelText('Search resources');
  fireEvent.change(input, { target: { value: term } });
  fireEvent.keyDown(input, { key: 'Enter' });
}

describe('ResourceSearch — draft/applied (no per-keystroke fetch)', () => {
  it('does not commit the term until Enter/Apply — typing alone leaves the hook term empty', () => {
    renderSearch();
    // initial render: the applied term is empty (the hook is disabled by an empty q).
    expect(lastParams().q).toBe('');

    const input = screen.getByLabelText('Search resources');
    fireEvent.change(input, { target: { value: 'stor' } });
    // DRAFT changed, but nothing committed → the hook still sees the empty applied term.
    expect(lastParams().q).toBe('');

    fireEvent.keyDown(input, { key: 'Enter' });
    // committed → the applied term now drives the hook.
    expect(lastParams().q).toBe('stor');
  });

  it('commits via the Apply button too', () => {
    renderSearch();
    const input = screen.getByLabelText('Search resources');
    fireEvent.change(input, { target: { value: 'vnet' } });
    fireEvent.click(screen.getByRole('button', { name: /Search/ }));
    expect(lastParams().q).toBe('vnet');
  });
});

describe('ResourceSearch — flat result list', () => {
  it('renders each hit as a flat row: name + short type + RG hint', () => {
    useResourceSearchMock.mockReturnValue(ok({ count: 2, value: [row1, row2] }));
    renderSearch();
    applyTerm('o');

    expect(screen.getByText('stapp01')).toBeTruthy();
    expect(screen.getByText('storageAccounts')).toBeTruthy(); // short type (last segment)
    expect(screen.getByText('vnet-demo')).toBeTruthy();
    expect(screen.getByText('virtualNetworks')).toBeTruthy();
    // the RG hint is rendered somewhere in the row
    expect(screen.getAllByText(/rg-app/).length).toBeGreaterThan(0);
  });

  it('shows the empty "no matches" state when a committed search returns nothing', () => {
    useResourceSearchMock.mockReturnValue(ok({ count: 0, value: [] }));
    renderSearch();
    applyTerm('zzz');
    expect(screen.getByText(/No resources match/i)).toBeTruthy();
  });
});

describe('ResourceSearch — Prev/Next (replace, not append)', () => {
  it('replaces the page on Next, returns on Prev, and disables the edges', () => {
    const nextLink = '/_sim/resources/search?q=o&%24skiptoken=RTOK2';
    useResourceSearchMock.mockImplementation((params: { skipToken?: string }) =>
      params.skipToken
        ? ok({ count: 2, value: [row2] })
        : ok({ count: 2, value: [row1], nextLink }),
    );
    renderSearch();
    applyTerm('o');

    expect(screen.getByText('stapp01')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Previous results' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Next results' }) as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Next results' }));

    expect(lastParams().skipToken).toBe('RTOK2');
    expect(screen.getByText('vnet-demo')).toBeTruthy();
    expect(screen.queryByText('stapp01')).toBeNull(); // REPLACE, not append
    expect((screen.getByRole('button', { name: 'Next results' }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: 'Previous results' }));
    expect(screen.getByText('stapp01')).toBeTruthy();
    expect(screen.queryByText('vnet-demo')).toBeNull();
  });
});

describe('ResourceSearch — deep-link select', () => {
  it('raises onSelectResource({ armId, sub, rg }) on a result-row click', () => {
    useResourceSearchMock.mockReturnValue(ok({ count: 1, value: [row1] }));
    const { onSelectResource } = renderSearch();
    applyTerm('sto');

    fireEvent.click(screen.getByRole('button', { name: /stapp01/ }));
    expect(onSelectResource).toHaveBeenCalledWith({ armId: ARM1, sub: 'sub-a', rg: 'rg-app' });
  });
});

describe('ResourceSearch — Subscriptions section (EXPL-GAP-01)', () => {
  it('renders a Subscriptions section from the backend `subscriptions` array and a row click raises onSelectSubscription(id)', () => {
    useResourceSearchMock.mockReturnValue(
      ok({
        count: 0,
        value: [],
        subscriptions: [
          { id: 'sub-a', name: 'Contoso-Prod-A' },
          { id: 'sub-b', name: 'Contoso-Dev-B' },
        ],
      }),
    );
    const { onSelectSubscription } = renderSearch();
    applyTerm('contoso');

    // The section is present and labelled distinctly from the resource results.
    const section = screen.getByLabelText('Subscription matches');
    expect(section).toBeTruthy();
    expect(within(section).getByText('Contoso-Prod-A')).toBeTruthy();
    expect(within(section).getByText('Contoso-Dev-B')).toBeTruthy();

    fireEvent.click(within(section).getByRole('button', { name: /Contoso-Prod-A/ }));
    expect(onSelectSubscription).toHaveBeenCalledWith('sub-a');
  });

  it('renders NO Subscriptions section when the array is empty (no empty header)', () => {
    useResourceSearchMock.mockReturnValue(ok({ count: 1, value: [row1], subscriptions: [] }));
    renderSearch();
    applyTerm('sto');
    expect(screen.queryByLabelText('Subscription matches')).toBeNull();
  });

  it('renders NO Subscriptions section before a term is applied', () => {
    useResourceSearchMock.mockReturnValue(
      ok({ count: 0, value: [], subscriptions: [{ id: 'sub-a', name: 'Contoso-Prod-A' }] }),
    );
    renderSearch();
    // no applyTerm() → nothing committed
    expect(screen.queryByLabelText('Subscription matches')).toBeNull();
  });
});

describe('ResourceSearch — Resource groups section (RG-name search)', () => {
  it('renders a Resource groups section from the backend `resourceGroups` array and a row click raises onSelectResourceGroup(sub, rg)', () => {
    // The gap this closes: an RG-name term matches ZERO resource rows (value: []) but the RG
    // surfaces in `resourceGroups[]` — so you CAN find rg-corp-dev-backup-43 among thousands.
    useResourceSearchMock.mockReturnValue(
      ok({
        count: 0,
        value: [],
        resourceGroups: [
          { name: 'rg-corp-dev-backup-43', subscriptionId: 'sub-a' },
          { name: 'rg-corp-prod-shared-04', subscriptionId: 'sub-b' },
        ],
      }),
    );
    const { onSelectResourceGroup } = renderSearch();
    applyTerm('rg-corp');

    const section = screen.getByLabelText('Resource group matches');
    expect(section).toBeTruthy();
    expect(within(section).getByText('rg-corp-dev-backup-43')).toBeTruthy();
    expect(within(section).getByText('rg-corp-prod-shared-04')).toBeTruthy();

    fireEvent.click(within(section).getByRole('button', { name: /rg-corp-dev-backup-43/ }));
    expect(onSelectResourceGroup).toHaveBeenCalledWith('sub-a', 'rg-corp-dev-backup-43');
  });

  it('renders NO Resource groups section when the array is empty (no empty header)', () => {
    useResourceSearchMock.mockReturnValue(ok({ count: 1, value: [row1], resourceGroups: [] }));
    renderSearch();
    applyTerm('sto');
    expect(screen.queryByLabelText('Resource group matches')).toBeNull();
  });

  it('renders NO Resource groups section before a term is applied', () => {
    useResourceSearchMock.mockReturnValue(
      ok({
        count: 0,
        value: [],
        resourceGroups: [{ name: 'rg-corp-dev-backup-43', subscriptionId: 'sub-a' }],
      }),
    );
    renderSearch();
    // no applyTerm() → nothing committed
    expect(screen.queryByLabelText('Resource group matches')).toBeNull();
  });
});
