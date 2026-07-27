/**
 * ConfirmDialog (D-10) — the shared PLAIN destructive confirm used across the control plane.
 *
 * Every destructive action (reset / delete snapshot / regenerate-over-existing / restore-over-active)
 * opens THIS dialog before the mutation fires. It is intentionally plain — NO typed confirmation
 * (D-10: typed confirmation was rejected as too heavy for a workflow operators run repeatedly).
 *
 * Accessibility (17-UI-SPEC §Accessibility): `role="dialog" aria-modal="true"` labelled by its title,
 * focus-trapped, Esc = Cancel, initial focus on Cancel (the destructive primary is NEVER the default
 * focus). The primary uses the `--red` treatment (red border + red text on transparent, not a filled
 * red block, per the restrained mockup). All styling is tokens-only (controls/tenants modules).
 */
import { useEffect, useRef, type KeyboardEvent } from 'react';

import controls from './controls.module.css';
import styles from './tenants.module.css';

export interface ConfirmDialogProps {
  title: string;
  body: string;
  primaryLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}

const TITLE_ID = 'confirm-dialog-title';

export default function ConfirmDialog({
  title,
  body,
  primaryLabel,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  // Initial focus on Cancel (never the destructive primary), returning focus to the invoker on close.
  useEffect(() => {
    const invoker = document.activeElement as HTMLElement | null;
    cancelRef.current?.focus();
    return () => invoker?.focus?.();
  }, []);

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onCancel();
      return;
    }
    if (e.key === 'Tab') {
      // Minimal focus trap: cycle Tab / Shift+Tab between the two dialog buttons.
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>('button');
      if (!focusable || focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }

  return (
    <div
      className={styles.overlay}
      onMouseDown={(e) => {
        // A click on the scrim (outside the dialog) cancels — the same non-destructive default as Esc.
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div
        ref={dialogRef}
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={TITLE_ID}
        onKeyDown={onKeyDown}
      >
        <h2 id={TITLE_ID} className={styles.dialogTitle}>
          {title}
        </h2>
        <p className={styles.dialogBody}>{body}</p>
        <div className={styles.dialogActions}>
          <button ref={cancelRef} type="button" className={controls.secondaryBtn} onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className={styles.dialogPrimary} onClick={onConfirm}>
            {primaryLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
