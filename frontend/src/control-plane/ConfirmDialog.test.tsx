import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

/**
 * ConfirmDialog (D-10) — the shared PLAIN destructive confirm (no typed confirmation).
 *
 * Pins the 17-UI-SPEC §Accessibility contract for the dialog:
 *  - it is a `role="dialog" aria-modal="true"` labelled by its title, carrying title + body copy
 *  - initial focus is on Cancel (the destructive primary is NEVER the default focus)
 *  - Esc = Cancel (calls onCancel, not onConfirm)
 *  - clicking the destructive primary fires onConfirm
 *  - there is NO typed-confirmation input (plain confirm, D-10)
 */

import ConfirmDialog from './ConfirmDialog';

function renderDialog(overrides: Partial<React.ComponentProps<typeof ConfirmDialog>> = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(
    <ConfirmDialog
      title="Reset to empty?"
      body="This wipes the active tenant."
      primaryLabel="Reset"
      onConfirm={onConfirm}
      onCancel={onCancel}
      {...overrides}
    />,
  );
  return { onConfirm, onCancel };
}

describe('ConfirmDialog — a plain, labelled modal (D-10)', () => {
  it('is an aria-modal dialog labelled by the title, carrying the title + body copy', () => {
    renderDialog();
    const dialog = screen.getByRole('dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(screen.getByText('Reset to empty?')).toBeTruthy();
    expect(screen.getByText('This wipes the active tenant.')).toBeTruthy();
  });

  it('puts initial focus on Cancel (the destructive primary is never the default focus)', () => {
    renderDialog();
    const cancel = screen.getByRole('button', { name: 'Cancel' });
    expect(document.activeElement).toBe(cancel);
  });

  it('has no typed-confirmation input (plain confirm, D-10)', () => {
    renderDialog();
    expect(screen.queryByRole('textbox')).toBeNull();
  });
});

describe('ConfirmDialog — cancel / confirm wiring', () => {
  it('Esc cancels (onCancel fires, onConfirm does not)', () => {
    const { onConfirm, onCancel } = renderDialog();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('clicking the destructive primary fires onConfirm', () => {
    const { onConfirm } = renderDialog();
    fireEvent.click(screen.getByRole('button', { name: 'Reset' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
