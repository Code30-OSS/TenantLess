import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

/**
 * TenantsManager (CTRL-03/CTRL-04) — the snapshots/reset manager.
 *
 * The control hooks + `useSummary` are mocked so the confirm-gated, busy-aware, safe-name-validated
 * behavior is asserted without a network:
 *  - `restore` on a row opens a PLAIN ConfirmDialog with the exact `Restore "{name}"?` copy; confirming
 *    calls `useRestoreSnapshot({ name })`
 *  - every row action + Save + Reset is disabled while a job runs (single-writer busy lock, D-11)
 *  - an unsafe save name shows the exact safe-name hint and disables the Save CTA (T-17-02)
 */

const {
  useSnapshotsMock,
  useSaveSnapshotMock,
  useRestoreSnapshotMock,
  useDeleteSnapshotMock,
  useResetMock,
  useJobMock,
  useSummaryMock,
  saveMutate,
  restoreMutate,
  deleteMutate,
  resetMutate,
} = vi.hoisted(() => ({
  useSnapshotsMock: vi.fn(),
  useSaveSnapshotMock: vi.fn(),
  useRestoreSnapshotMock: vi.fn(),
  useDeleteSnapshotMock: vi.fn(),
  useResetMock: vi.fn(),
  useJobMock: vi.fn(),
  useSummaryMock: vi.fn(),
  saveMutate: vi.fn(),
  restoreMutate: vi.fn(),
  deleteMutate: vi.fn(),
  resetMutate: vi.fn(),
}));

vi.mock('../api/control', () => ({
  useSnapshots: useSnapshotsMock,
  useSaveSnapshot: useSaveSnapshotMock,
  useRestoreSnapshot: useRestoreSnapshotMock,
  useDeleteSnapshot: useDeleteSnapshotMock,
  useReset: useResetMock,
  useJob: useJobMock,
}));
vi.mock('../api/queries', () => ({ useSummary: useSummaryMock }));

import TenantsManager from './TenantsManager';

beforeEach(() => {
  useSnapshotsMock
    .mockReset()
    .mockReturnValue({ data: [{ name: 's1', createdUnix: 1_752_019_200 }], isLoading: false });
  useSaveSnapshotMock.mockReset().mockReturnValue({ mutate: saveMutate, isPending: false, error: null });
  useRestoreSnapshotMock
    .mockReset()
    .mockReturnValue({ mutate: restoreMutate, isPending: false, error: null });
  useDeleteSnapshotMock
    .mockReset()
    .mockReturnValue({ mutate: deleteMutate, isPending: false, error: null });
  useResetMock.mockReset().mockReturnValue({ mutate: resetMutate, isPending: false, error: null });
  useJobMock.mockReset().mockReturnValue({ data: undefined });
  useSummaryMock
    .mockReset()
    .mockReturnValue({ data: { tenantId: 't-active', totals: { subscriptions: 1, resourceGroups: 1, resources: 10, violations: 0 } } });
  saveMutate.mockReset();
  restoreMutate.mockReset();
  deleteMutate.mockReset();
  resetMutate.mockReset();
});

function renderManager(props: Partial<React.ComponentProps<typeof TenantsManager>> = {}) {
  render(<TenantsManager busy={false} activeJobId={null} onStarted={vi.fn()} {...props} />);
}

describe('TenantsManager — restore is confirm-gated (D-10)', () => {
  it('opens the "Restore \\"s1\\"?" confirm and calls useRestoreSnapshot on confirm', () => {
    renderManager();
    fireEvent.click(screen.getByRole('button', { name: 'restore' }));

    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(screen.getByText('Restore "s1"?')).toBeTruthy();

    // Dialog primary is the capitalized "Restore" (distinct from the lowercase row action).
    fireEvent.click(screen.getByRole('button', { name: 'Restore' }));
    expect(restoreMutate).toHaveBeenCalledTimes(1);
    expect(restoreMutate.mock.calls[0][0]).toMatchObject({ name: 's1' });
  });

  it('cancelling the confirm does NOT restore', () => {
    renderManager();
    fireEvent.click(screen.getByRole('button', { name: 'restore' }));
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(restoreMutate).not.toHaveBeenCalled();
  });
});

describe('TenantsManager — single-writer busy lock (D-11)', () => {
  it('disables every row action + Save + Reset while a job runs', () => {
    renderManager({ busy: true });
    expect((screen.getByRole('button', { name: 'restore' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'delete' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: /save snapshot/i }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: /reset to empty/i }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('TenantsManager — safe-name validation (T-17-02)', () => {
  it('rejects an unsafe save name with the exact hint and disables Save', () => {
    renderManager();
    const input = screen.getByLabelText(/snapshot name/i);
    fireEvent.change(input, { target: { value: '../x' } });
    fireEvent.blur(input);

    expect(
      screen.getByText('Use letters, numbers, dashes or underscores only — no paths.'),
    ).toBeTruthy();
    expect((screen.getByRole('button', { name: /save snapshot/i }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });
});
