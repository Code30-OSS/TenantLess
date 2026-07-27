import { describe, it, expect, vi, beforeEach } from 'vitest';
import { StrictMode } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';

import type { Dependency, DependenciesResponse } from '../api/types';
// Raw source (Vite `?raw`) for the WR-03 structural anti-pattern assertion below.
import dependenciesViewSource from './DependenciesView.tsx?raw';
import filterBarSource from './FilterBar.tsx?raw';

/**
 * EXPL-04 — the cross-subscription dependency edge table view. `useDependencies` is mocked so the
 * paging / range-readout behavior is asserted against the exact hook arguments, without a network.
 *
 * WR-04: the "start–end of count" readout must use the FIXED page size, so a short final page reads
 * the correct absolute range (not `pageIndex * currentPageRows + 1`).
 */

const { useDependenciesMock } = vi.hoisted(() => ({ useDependenciesMock: vi.fn() }));
vi.mock('../api/queries', () => ({ useDependencies: useDependenciesMock }));

import DependenciesView from './DependenciesView';

function edge(i: number): Dependency {
  return {
    type: 'private-endpoint',
    source: { resourceId: `/subscriptions/sub-a/rg/src${i}`, subscriptionId: 'sub-a' },
    target: { resourceId: `/subscriptions/sub-b/rg/tgt${i}`, subscriptionId: 'sub-b' },
    crossSubscription: false,
  };
}

function page(n: number, nextToken: string | null, count: number): DependenciesResponse {
  return {
    count,
    value: Array.from({ length: n }, (_, i) => edge(i)),
    nextLink: nextToken ? `/_sim/dependencies?$skiptoken=${nextToken}` : undefined,
  };
}

function result(data: DependenciesResponse) {
  return { data, isLoading: false, isError: false, error: null, refetch: vi.fn() };
}

/**
 * Three server pages of a 220-edge set: page1 (100, →TOK1), page2 (100, →TOK2), page3 (20, last).
 * The mock keys off the `skipToken` the view requests, mirroring the server's keyset paging.
 */
function wireThreePages() {
  useDependenciesMock.mockImplementation((params?: { skipToken?: string }) => {
    if (params?.skipToken === 'TOK1') return result(page(100, 'TOK2', 220));
    if (params?.skipToken === 'TOK2') return result(page(20, null, 220));
    return result(page(100, 'TOK1', 220));
  });
}

beforeEach(() => {
  useDependenciesMock.mockReset();
});

function renderStrict() {
  render(
    <StrictMode>
      <DependenciesView />
    </StrictMode>,
  );
}

/** The last params object passed to useDependencies. */
function lastDepParams() {
  const calls = useDependenciesMock.mock.calls;
  return calls[calls.length - 1][0] as { subscription?: string; type?: string; skipToken?: string };
}

/** Commit the draft filter via the explicit Apply affordance. */
function applyFilter() {
  fireEvent.click(screen.getByRole('button', { name: /apply filter/i }));
}

const FULL_UUID = 'b7e2c1a0-1234-4abc-8def-0123456789ab';

describe('DependenciesView — deliberate apply (UAT Gap 6)', () => {
  it('a partial UUID is never handed to useDependencies (no invalid-UUID request while typing)', () => {
    useDependenciesMock.mockReturnValue(result(page(1, null, 1)));
    renderStrict();

    // type an obviously-incomplete subscription, then explicitly apply
    fireEvent.change(screen.getByLabelText('subscription'), { target: { value: 'b7e2c1a0-12' } });
    applyFilter();

    // the guard rejects the partial value: it must never reach the hook
    const sawPartial = useDependenciesMock.mock.calls.some(
      ([p]) => p?.subscription === 'b7e2c1a0-12',
    );
    expect(sawPartial).toBe(false);
    expect(lastDepParams().subscription).toBeUndefined();
  });

  it('typing does not fire a request per keystroke (draft only until apply)', () => {
    useDependenciesMock.mockReturnValue(result(page(1, null, 1)));
    renderStrict();

    fireEvent.change(screen.getByLabelText('type'), { target: { value: 'priv' } });
    // not applied — the draft has not reached the query key
    expect(lastDepParams().type).toBeUndefined();
  });

  it('applying a full value commits the subscription and type to useDependencies', () => {
    useDependenciesMock.mockReturnValue(result(page(1, null, 1)));
    renderStrict();

    fireEvent.change(screen.getByLabelText('subscription'), { target: { value: FULL_UUID } });
    fireEvent.change(screen.getByLabelText('type'), { target: { value: 'private-endpoint' } });
    applyFilter();

    expect(useDependenciesMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ subscription: FULL_UUID, type: 'private-endpoint' }),
    );
  });
});

describe('DependenciesView — WR-04 pager range uses the fixed page size', () => {
  it('reads the correct absolute range on a short final page (not pageIndex * currentRows)', () => {
    wireThreePages();
    renderStrict();

    // page 1: 100 rows of 220
    expect(screen.getByTestId('dep-range').textContent).toBe('1–100 of 220');

    fireEvent.click(screen.getByRole('button', { name: /next/ }));
    // page 2: 100 rows, 101–200
    expect(screen.getByTestId('dep-range').textContent).toBe('101–200 of 220');

    fireEvent.click(screen.getByRole('button', { name: /next/ }));
    // page 3: only 20 rows — must read 201–220, NOT 2*20+1=41
    expect(screen.getByTestId('dep-range').textContent).toBe('201–220 of 220');
  });
});

describe('WR-03 — goPrev is a pure transition (no setState nested in a setState updater)', () => {
  // A state-updater must be pure. `main.tsx` enables <StrictMode>, which double-invokes updaters in
  // dev; a setSkipToken nested inside a setPrevTokens updater fires as a side effect during that
  // double-invoke. It is idempotent today (fragile), so we pin the STRUCTURE: no setter is nested
  // inside another setter's block-body updater in either paginated view.
  const NESTED_SETTER =
    /set(?:PrevTokens|SkipToken)\(\s*\([^)]*\)\s*=>\s*\{[\s\S]*?set(?:SkipToken|PrevTokens)\s*\(/;

  it.each([
    ['DependenciesView.tsx', dependenciesViewSource],
    ['FilterBar.tsx', filterBarSource],
  ])('%s does not nest a paging setter inside another setter’s updater', (_name, source) => {
    expect(NESTED_SETTER.test(source)).toBe(false);
  });

  it('DependenciesView navigates prev correctly and requests the prior skipToken (StrictMode-safe)', () => {
    wireThreePages();
    renderStrict();

    fireEvent.click(screen.getByRole('button', { name: /next/ })); // → TOK1
    fireEvent.click(screen.getByRole('button', { name: /next/ })); // → TOK2
    expect(useDependenciesMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ skipToken: 'TOK2' }),
    );

    fireEvent.click(screen.getByRole('button', { name: /prev/ })); // back to TOK1
    expect(useDependenciesMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ skipToken: 'TOK1' }),
    );

    fireEvent.click(screen.getByRole('button', { name: /prev/ })); // back to page 1 (no token)
    expect(useDependenciesMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ skipToken: undefined }),
    );

    expect(screen.getByRole('button', { name: /prev/ })).toHaveProperty('disabled', true);
  });
});
