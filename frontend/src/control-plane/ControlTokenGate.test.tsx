import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import { ArmError } from '../api/client';

/**
 * ControlTokenGate (CTRL-05, T-17-05) — the in-memory-token unlock/lock surface.
 *
 * The control fetch layer is mocked so the gate's behavior is asserted without a network: unlock sets
 * the in-memory token then probes `/_control/probe`; a 2xx accepts (onUnlocked), a 401/403 CLEARS the
 * token (setControlToken(null)) and surfaces the ApiError message in a `role="alert"` red banner.
 *
 * Pins:
 *  - a successful probe sets the entered token and calls onUnlocked
 *  - a 401 probe clears the token (last setControlToken call is null) and shows the ApiError message
 *  - the field is type=password (the token is never echoed)
 */

const { controlGetMock, setControlTokenMock } = vi.hoisted(() => ({
  controlGetMock: vi.fn(),
  setControlTokenMock: vi.fn(),
}));

vi.mock('../api/control', () => ({
  controlGet: controlGetMock,
  setControlToken: setControlTokenMock,
  isAuthError: (e: unknown) => e instanceof ArmError && (e.status === 401 || e.status === 403),
}));

import ControlTokenGate from './ControlTokenGate';

beforeEach(() => {
  controlGetMock.mockReset();
  setControlTokenMock.mockReset();
});

function enterToken(value: string) {
  fireEvent.change(screen.getByLabelText(/control token/i), { target: { value } });
}

function clickUnlock() {
  fireEvent.click(screen.getByRole('button', { name: /unlock control plane/i }));
}

describe('ControlTokenGate — unlock success', () => {
  it('sets the entered token and calls onUnlocked when the probe returns 2xx', async () => {
    controlGetMock.mockResolvedValue({ armed: true });
    const onUnlocked = vi.fn();
    render(<ControlTokenGate onUnlocked={onUnlocked} />);

    enterToken('s3cret-token');
    clickUnlock();

    await waitFor(() => expect(onUnlocked).toHaveBeenCalledTimes(1));
    expect(setControlTokenMock).toHaveBeenCalledWith('s3cret-token');
    expect(controlGetMock).toHaveBeenCalledWith('/_control/probe');
  });
});

describe('ControlTokenGate — invalid token (401 clears + red banner)', () => {
  it('clears the in-memory token and shows the ApiError message in a role=alert banner', async () => {
    controlGetMock.mockRejectedValue(new ArmError('Unauthorized', 'Invalid control token.', 401));
    const onUnlocked = vi.fn();
    render(<ControlTokenGate onUnlocked={onUnlocked} />);

    enterToken('wrong-token');
    clickUnlock();

    const banner = await screen.findByRole('alert');
    expect(banner.textContent).toContain('Invalid control token.');
    // token was cleared: the LAST setControlToken call is null (re-lock)
    expect(setControlTokenMock).toHaveBeenLastCalledWith(null);
    expect(onUnlocked).not.toHaveBeenCalled();
  });
});

describe('ControlTokenGate — token field posture', () => {
  it('renders the token field as type=password (never echoed)', () => {
    render(<ControlTokenGate onUnlocked={vi.fn()} />);
    const field = screen.getByLabelText(/control token/i) as HTMLInputElement;
    expect(field.type).toBe('password');
  });
});
