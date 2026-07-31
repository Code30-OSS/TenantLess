import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';

/**
 * ControlPlaneView (follow-up) — the control-plane lock button must be disabled
 * while a control job is `busy` (queued/running).
 *
 * Why this matters: the app-level JobProvider polls the active job across routes via
 * `useJob` → `controlGet('/_control/jobs/{id}')`, which attaches `X-Control-Token`. `lock()`
 * calls `setControlToken(null)`, so tearing down control auth mid-job makes the poll 401 → the
 * job's `succeeded` edge is never observed → no full invalidation → the Explorer can go stale
 * again. The fix (single-writer, D-11): you cannot lock while a write job is in flight.
 *
 * Seams (repo idiom, cf. ControlTokenGate.test.tsx):
 *  - `../api/control` mocked: `controlGet` RESOLVES (2xx probe → armed + unlocked → lock strip
 *    renders), `setControlToken` a no-op.
 *  - `./JobContext` mocked: `useJobContext()` reads a hoisted mutable `state.busy` so a rerender
 *    reflects the flipped busy state without a live JobProvider + form interaction.
 *  - `./GenerateForm` stubbed to null: SectionBody mounts it for the default 'generate' section;
 *    stubbing keeps the render light and avoids its react-query hooks (no QueryClientProvider).
 *
 * NO jest-dom in this repo — disabled is asserted via `(btn as HTMLButtonElement).disabled`.
 */

const { controlGetMock, setControlTokenMock, state } = vi.hoisted(() => ({
  controlGetMock: vi.fn(),
  setControlTokenMock: vi.fn(),
  state: { busy: true as boolean },
}));

vi.mock('../api/control', () => ({
  controlGet: controlGetMock,
  setControlToken: setControlTokenMock,
}));

vi.mock('./JobContext', () => ({
  useJobContext: () => ({
    activeJobId: null,
    activeJob: undefined,
    busy: state.busy,
    reportJob: vi.fn(),
  }),
}));

vi.mock('./GenerateForm', () => ({ default: () => null }));

import ControlPlaneView from './ControlPlaneView';

beforeEach(() => {
  controlGetMock.mockReset();
  setControlTokenMock.mockReset();
  // A resolved probe → armed + unlocked → the lock strip (with the lock button) renders.
  controlGetMock.mockResolvedValue({ armed: true });
  state.busy = true;
});

describe('ControlPlaneView — lock button busy guard (P2)', () => {
  it('disables the lock button while a job is busy, and re-enables it at terminal', async () => {
    const { rerender } = render(
      <MemoryRouter>
        <ControlPlaneView />
      </MemoryRouter>,
    );

    // busy=true → the operator MUST NOT be able to tear down control auth mid-job.
    const lockBtn = await screen.findByRole('button', { name: /^lock$/i });
    expect((lockBtn as HTMLButtonElement).disabled).toBe(true);

    // The active job reaches a terminal status → busy=false → locking is allowed again.
    state.busy = false;
    rerender(
      <MemoryRouter>
        <ControlPlaneView />
      </MemoryRouter>,
    );

    const lockBtnAfter = await screen.findByRole('button', { name: /^lock$/i });
    expect((lockBtnAfter as HTMLButtonElement).disabled).toBe(false);
  });
});
