import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

/**
 * ViewerView (S3 — DEMO-03, 18-UI-SPEC §S3) — the live ARM response viewer.
 *
 * Pins the four-state contract + real-id sourcing WITHOUT a network: `../api/client` is partially
 * mocked so ONLY `armGet` is a spy (the real `ArmError`/`assertSameOrigin` are kept via importActual),
 * and the three simGet-backed hooks (`useSubscriptions`/`useResourceSearch`/`useSummary`) are mocked —
 * while `../api/queries`' pure `resourceGroupsUrl`/`resourcesUrl` builders (consumed by `endpoints.ts`)
 * are preserved via importActual so path composition still runs for real.
 *
 *  - Success + id-sourcing: with a real full ARM id from `useResourceSearch` and `armGet` resolving a
 *    body, selecting the resource-detail endpoint + Run renders the `200` status strip (carrying the
 *    sourced id) and a `JsonTree` (`role="tree"`), and `armGet` is called with that real id.
 *  - Empty tenant: `useSummary` reports 0 subscriptions → the CTA card + a link resolving to
 *    `/ui/control-plane/generate` (never a 404 / broken select).
 *  - Error: `armGet` rejects an `ArmError('ResourceNotFound', …, 404)` → the `--red` errorRow renders
 *    `ResourceNotFound — …`; no auth-fail path is exercised (any-Bearer).
 */

const { armGetMock } = vi.hoisted(() => ({ armGetMock: vi.fn() }));
vi.mock('../api/client', async (importActual) => {
  const actual = await importActual<typeof import('../api/client')>();
  return { ...actual, armGet: armGetMock };
});

const { useSubscriptionsMock, useResourceSearchMock, useSummaryMock } = vi.hoisted(() => ({
  useSubscriptionsMock: vi.fn(),
  useResourceSearchMock: vi.fn(),
  useSummaryMock: vi.fn(),
}));
vi.mock('../api/queries', async (importActual) => {
  const actual = await importActual<typeof import('../api/queries')>();
  return {
    ...actual,
    useSubscriptions: useSubscriptionsMock,
    useResourceSearch: useResourceSearchMock,
    useSummary: useSummaryMock,
  };
});

import { ArmError } from '../api/client';
import ViewerView from './ViewerView';

const SUB_ID = 'sub-1111';
const ARM_ID =
  '/subscriptions/sub-1111/resourceGroups/rg-77/providers/Microsoft.Compute/virtualMachines/vm-9';

function renderViewer() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter basename="/ui" initialEntries={['/ui/demo/viewer']}>
        <ViewerView />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Tenant present: one subscription + one resource-search hit carrying a full ARM id. */
function seedActiveTenant() {
  useSummaryMock.mockReturnValue({ data: { totals: { subscriptions: 3 } } });
  useSubscriptionsMock.mockReturnValue({
    data: {
      count: 3,
      value: [
        {
          subscriptionId: SUB_ID,
          name: 'prod',
          archetype: 'enterprise',
          resourceCount: 100,
          resourceGroupCount: 5,
          violationCount: 2,
        },
      ],
    },
  });
  useResourceSearchMock.mockReturnValue({
    data: {
      count: 1,
      value: [
        {
          id: ARM_ID,
          name: 'vm-9',
          type: 'Microsoft.Compute/virtualMachines',
          subscriptionId: SUB_ID,
          resourceGroupName: 'rg-77',
        },
      ],
    },
  });
}

beforeEach(() => {
  armGetMock.mockReset();
  useSummaryMock.mockReset();
  useSubscriptionsMock.mockReset();
  useResourceSearchMock.mockReset();
});

describe('ViewerView — DEMO-03 success + id sourcing', () => {
  it('runs a live armGet with a real sourced id and renders the 200 status strip + JsonTree', async () => {
    seedActiveTenant();
    armGetMock.mockResolvedValue({ value: [{ id: ARM_ID, name: 'vm-9' }] });

    renderViewer();

    fireEvent.change(screen.getByLabelText('ENDPOINT'), { target: { value: 'resource-detail' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run request' }));

    const strip = await screen.findByTestId('status-strip');
    expect(strip.textContent).toContain('200');
    // The live call composed its path from the simGet-backed resource-search hit (real full ARM id).
    expect(strip.textContent).toContain(SUB_ID);
    expect(armGetMock).toHaveBeenCalledTimes(1);
    expect(armGetMock.mock.calls[0][0]).toBe(ARM_ID);

    // The ARM response body renders through the reused lazy JsonTree.
    expect(screen.getByRole('tree')).toBeTruthy();
  });
});

describe('ViewerView — DEMO-03 empty tenant (0 subscriptions)', () => {
  it('shows the generate CTA card linking to /control-plane/generate, not a 404', () => {
    useSummaryMock.mockReturnValue({ data: { totals: { subscriptions: 0 } } });
    useSubscriptionsMock.mockReturnValue({ data: { count: 0, value: [] } });
    useResourceSearchMock.mockReturnValue({ data: { count: 0, value: [] } });

    renderViewer();

    expect(screen.getByText(/No tenant is loaded/i)).toBeTruthy();
    const cta = screen.getByRole('link', { name: /control plane/i });
    expect(cta.getAttribute('href')).toBe('/ui/control-plane/generate');
    // Run is a no-op on an empty tenant (never fires an unresolvable request).
    expect(screen.getByRole('button', { name: 'Run request' }).hasAttribute('disabled')).toBe(true);
  });
});

describe('ViewerView — DEMO-03 error state', () => {
  it('renders the ArmError code — message in the red error row (no auth-fail path)', async () => {
    seedActiveTenant();
    armGetMock.mockRejectedValue(
      new ArmError('ResourceNotFound', 'The resource could not be found.', 404),
    );

    renderViewer();

    fireEvent.change(screen.getByLabelText('ENDPOINT'), { target: { value: 'resource-detail' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run request' }));

    const row = await screen.findByTestId('error-row');
    expect(row.textContent).toContain('ResourceNotFound —');
    expect(row.textContent).toContain('The resource could not be found.');
  });
});
