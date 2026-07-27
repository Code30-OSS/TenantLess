import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

/**
 * GenerateForm (CTRL-01, D-08/D-03) — fields map 1:1 to the `generate` flags with client caps.
 *
 * The control hooks are mocked so the map/validate/submit behavior is asserted without a network:
 *  - an out-of-cap field shows the EXACT UI-SPEC copy and disables the CTA
 *  - a valid submit calls `useStartGenerate` with the mapped `GenerateArgs` (violations bool from the
 *    slider, over_privilege bool from the toggle)
 *  - the PROFILE select is populated from `useProfiles` (bundled + derived), not a hardcoded list
 *  - a busy container disables the whole form (single-writer, D-11)
 */

const { useProfilesMock, useStartGenerateMock, useJobMock, useSummaryMock, mutateMock } = vi.hoisted(
  () => ({
    useProfilesMock: vi.fn(),
    useStartGenerateMock: vi.fn(),
    useJobMock: vi.fn(),
    useSummaryMock: vi.fn(),
    mutateMock: vi.fn(),
  }),
);

vi.mock('../api/control', () => ({
  useProfiles: useProfilesMock,
  useStartGenerate: useStartGenerateMock,
  useJob: useJobMock,
}));
vi.mock('../api/queries', () => ({ useSummary: useSummaryMock }));

import GenerateForm from './GenerateForm';

beforeEach(() => {
  useProfilesMock
    .mockReset()
    .mockReturnValue({ data: [{ name: 'enterprise' }, { name: 'small' }], isLoading: false });
  useStartGenerateMock
    .mockReset()
    .mockReturnValue({ mutate: mutateMock, isPending: false, error: null });
  useJobMock.mockReset().mockReturnValue({ data: undefined });
  // Default: no active tenant (empty) → generate submits directly (nothing to replace).
  useSummaryMock.mockReset().mockReturnValue({ data: undefined });
  mutateMock.mockReset();
});

function renderForm(props: Partial<React.ComponentProps<typeof GenerateForm>> = {}) {
  render(<GenerateForm busy={false} activeJobId={null} onStarted={vi.fn()} {...props} />);
}

function cta() {
  return screen.getByRole('button', { name: /generate tenant/i }) as HTMLButtonElement;
}

describe('GenerateForm — client caps (exact UI-SPEC copy)', () => {
  it('shows the resources cap copy on blur and disables the CTA', () => {
    renderForm();
    const res = screen.getByLabelText(/target resources/i);
    fireEvent.change(res, { target: { value: '600000' } });
    fireEvent.blur(res);

    expect(screen.getByText('Target resources must be between 1 and 500,000.')).toBeTruthy();
    expect(cta().disabled).toBe(true);
  });
});

describe('GenerateForm — valid submit maps to GenerateArgs', () => {
  it('calls useStartGenerate with the mapped args (violations from slider, over_privilege from toggle)', () => {
    renderForm();
    fireEvent.change(screen.getByLabelText(/profile/i), { target: { value: 'enterprise' } });
    fireEvent.change(screen.getByLabelText(/seed/i), { target: { value: '42' } });
    fireEvent.change(screen.getByLabelText(/subscriptions/i), { target: { value: '10' } });
    fireEvent.change(screen.getByLabelText(/target resources/i), { target: { value: '5000' } });
    // jobs left at its default (1) so it is always ≤ cores; slider default 12% ⇒ violations on;
    // over-privilege toggle defaults on.

    fireEvent.click(cta());

    expect(mutateMock).toHaveBeenCalledTimes(1);
    const args = mutateMock.mock.calls[0][0];
    expect(args).toMatchObject({
      profile: 'enterprise',
      seed: 42,
      subscriptions: 10,
      resources: 5000,
      jobs: 1,
      violations: true,
      over_privilege: true,
    });
  });

  it('maps a 0% violation slider to violations:false', () => {
    renderForm();
    fireEvent.change(screen.getByLabelText(/profile/i), { target: { value: 'small' } });
    fireEvent.change(screen.getByLabelText(/subscriptions/i), { target: { value: '3' } });
    fireEvent.change(screen.getByLabelText(/target resources/i), { target: { value: '100' } });
    fireEvent.change(screen.getByLabelText(/injection rate/i), { target: { value: '0' } });

    fireEvent.click(cta());

    expect(mutateMock.mock.calls[0][0]).toMatchObject({ violations: false });
  });
});

describe('GenerateForm — PROFILE select is server-populated', () => {
  it('renders options from useProfiles, not a hardcoded list', () => {
    renderForm();
    expect(screen.getByRole('option', { name: 'enterprise' })).toBeTruthy();
    expect(screen.getByRole('option', { name: 'small' })).toBeTruthy();

    // a different server list yields different options (proves it is not hardcoded)
    useProfilesMock.mockReturnValue({ data: [{ name: 'derived-x' }], isLoading: false });
    renderForm();
    expect(screen.getByRole('option', { name: 'derived-x' })).toBeTruthy();
  });
});

describe('GenerateForm — D-10 regenerate-over-active confirm', () => {
  function fillValid() {
    fireEvent.change(screen.getByLabelText(/profile/i), { target: { value: 'enterprise' } });
    fireEvent.change(screen.getByLabelText(/subscriptions/i), { target: { value: '10' } });
    fireEvent.change(screen.getByLabelText(/target resources/i), { target: { value: '5000' } });
  }

  it('opens the Regenerate confirm before starting when a tenant is active', () => {
    useSummaryMock.mockReturnValue({
      data: { tenantId: 't-1', totals: { subscriptions: 1, resourceGroups: 1, resources: 10, violations: 0 } },
    });
    renderForm();
    fillValid();
    fireEvent.click(cta());

    // Nothing started yet — the plain confirm must gate the destructive regenerate.
    expect(mutateMock).not.toHaveBeenCalled();
    expect(screen.getByText('Regenerate tenant?')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Regenerate' }));
    expect(mutateMock).toHaveBeenCalledTimes(1);
  });

  it('submits directly (no confirm) on an empty tenant', () => {
    useSummaryMock.mockReturnValue({ data: undefined });
    renderForm();
    fillValid();
    fireEvent.click(cta());

    expect(mutateMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Regenerate tenant?')).toBeNull();
  });
});

describe('GenerateForm — single-writer busy lock (D-11)', () => {
  it('disables the CTA and reads "Busy — a job is running" while busy', () => {
    renderForm({ busy: true });
    const button = screen.getByRole('button', { name: /busy — a job is running/i }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });
});
