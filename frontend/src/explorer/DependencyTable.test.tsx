import { describe, it, expect } from 'vitest';
import { render, fireEvent } from '@testing-library/react';

import type { Dependency } from '../api/types';
import DependencyTable from './DependencyTable';

/**
 * EXPL-04 — the presentational cross-subscription edge table (D-02: an edge TABLE, not a graph).
 * No network here: the table takes an `edges` array and owns its sort state, so the row-render /
 * cross-sub gold accent / sortable-header behaviors are unit-testable directly.
 *
 * Pins:
 *  - one row per edge with Source, →, Target, Type and a Cross-sub badge
 *  - a crossSubscription=true row carries the gold accent (var(--gold)); a same-sub row does not
 *  - clicking the Type header sorts by type; clicking again reverses; sorting Source orders by source.resourceId
 *  - the legend row shows the ● cross-subscription edge marker
 */

const SUB_A = 'b7e2-1c4a';
const SUB_B = 'a1d0-9e22';

// Initial array order is deliberately NOT sorted by type or source, so a sort click reorders.
const edges: Dependency[] = [
  {
    // same-sub, type "reads", source resource name "web-zeta"
    type: 'reads',
    source: {
      resourceId: '/subscriptions/b7e2/resourceGroups/rg1/providers/Microsoft.Web/sites/web-zeta',
      subscriptionId: SUB_A,
    },
    target: {
      resourceId: '/subscriptions/b7e2/resourceGroups/rg1/providers/Microsoft.Sql/servers/sql-main',
      subscriptionId: SUB_A,
    },
    crossSubscription: false,
  },
  {
    // cross-sub, type "peering", source resource name "vnet-alpha"
    type: 'peering',
    source: {
      resourceId:
        '/subscriptions/b7e2/resourceGroups/rg1/providers/Microsoft.Network/virtualNetworks/vnet-alpha',
      subscriptionId: SUB_A,
    },
    target: {
      resourceId:
        '/subscriptions/a1d0/resourceGroups/rg2/providers/Microsoft.Network/virtualNetworks/vnet-spoke',
      subscriptionId: SUB_B,
    },
    crossSubscription: true,
  },
];

function bodyRows(container: HTMLElement): HTMLTableRowElement[] {
  return Array.from(container.querySelectorAll('tbody tr'));
}

describe('DependencyTable — rows', () => {
  it('renders one row per edge with source, target, type and a cross-sub badge', () => {
    const { container, getByText } = render(<DependencyTable edges={edges} />);
    const rows = bodyRows(container);
    expect(rows).toHaveLength(2);

    // source + target resource names + the dependency types are rendered
    expect(getByText('web-zeta')).toBeTruthy();
    expect(getByText('sql-main')).toBeTruthy();
    expect(getByText('vnet-alpha')).toBeTruthy();
    expect(getByText('vnet-spoke')).toBeTruthy();
    expect(getByText('reads')).toBeTruthy();
    expect(getByText('peering')).toBeTruthy();
  });
});

describe('DependencyTable — cross-subscription gold accent (D-02)', () => {
  it('gold-accents a cross-sub row and leaves a same-sub row unaccented', () => {
    const { container } = render(<DependencyTable edges={edges} />);
    const rows = bodyRows(container);
    const sameSub = rows.find((r) => r.getAttribute('data-cross-sub') === 'false')!;
    const crossSub = rows.find((r) => r.getAttribute('data-cross-sub') === 'true')!;

    expect(sameSub).toBeTruthy();
    expect(crossSub).toBeTruthy();
    // the cross-sub row references the gold token; the same-sub row carries no gold accent
    expect(crossSub.getAttribute('style')).toContain('var(--gold)');
    expect(sameSub.getAttribute('style') ?? '').not.toContain('var(--gold)');
  });
});

describe('DependencyTable — sortable columns', () => {
  it('sorts by Type ascending on the first header click and reverses on the second', () => {
    const { container, getByRole } = render(<DependencyTable edges={edges} />);
    const typeHeader = getByRole('button', { name: /type/i });

    fireEvent.click(typeHeader); // asc: peering < reads
    let rows = bodyRows(container);
    expect(rows[0].textContent).toContain('peering');
    expect(rows[1].textContent).toContain('reads');

    fireEvent.click(typeHeader); // desc: reads, peering
    rows = bodyRows(container);
    expect(rows[0].textContent).toContain('reads');
    expect(rows[1].textContent).toContain('peering');
  });

  it('sorts by Source (source.resourceId) on the Source header click', () => {
    const { container, getByRole } = render(<DependencyTable edges={edges} />);
    fireEvent.click(getByRole('button', { name: /source/i })); // asc by resourceId

    const rows = bodyRows(container);
    // "...Microsoft.Network/.../vnet-alpha" < "...Microsoft.Web/.../web-zeta"
    expect(rows[0].textContent).toContain('vnet-alpha');
    expect(rows[1].textContent).toContain('web-zeta');
  });
});

describe('DependencyTable — legend', () => {
  it('renders the ● cross-subscription edge marker in the legend', () => {
    const { getByText } = render(<DependencyTable edges={edges} />);
    const legend = getByText(/cross-subscription edge/i);
    expect(legend).toBeTruthy();
    expect(legend.textContent).toContain('●');
  });
});
