/**
 * ControlTokenGate (CTRL-05, T-17-05) — the in-memory control-token unlock panel.
 *
 * The control plane is a distinct auth realm (`X-Control-Token`, D-01/D-17). This panel takes a
 * password-type token, stores it via `setControlToken` (in-memory ONLY — never localStorage, cookie,
 * URL, or log), and probes `GET /_control/probe`: a 2xx accepts (→ `onUnlocked`), while a 401/403
 * CLEARS the token (`setControlToken(null)`, re-lock) and surfaces the ApiError message verbatim in a
 * `role="alert"` red banner. The token value is never echoed (type=password, autocomplete=off).
 */
import { useState, type FormEvent } from 'react';

import { ArmError } from '../api/client';
import { controlGet, setControlToken } from '../api/control';
import { PrimaryButton, TextField } from './fields';
import styles from './controls.module.css';

export default function ControlTokenGate({ onUnlocked }: { onUnlocked: () => void }) {
  const [token, setToken] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function unlock(e: FormEvent) {
    e.preventDefault();
    const candidate = token.trim();
    if (!candidate || submitting) return;

    setSubmitting(true);
    setError(null);
    // Store the candidate so the probe carries the X-Control-Token header, then verify it.
    setControlToken(candidate);
    try {
      await controlGet('/_control/probe');
      onUnlocked();
    } catch (err) {
      // Any rejection (401/403 or otherwise) re-locks the plane: the token was not accepted.
      setControlToken(null);
      setToken('');
      setError(err instanceof ArmError ? err.message : 'Invalid control token.');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className={styles.tokenPanel} onSubmit={unlock}>
      <div className={styles.tokenRow}>
        <TextField
          id="control-token"
          label="CONTROL TOKEN"
          type="password"
          autoComplete="off"
          value={token}
          onChange={setToken}
          disabled={submitting}
        />
        <PrimaryButton type="submit" disabled={submitting || token.trim() === ''}>
          {submitting ? 'Unlocking…' : 'Unlock control plane'}
        </PrimaryButton>
      </div>

      <p className={styles.tokenHelper}>
        The token is held in this browser session only and is never stored.
      </p>

      {error && (
        <div className={styles.errorRow} role="alert">
          <span className={styles.errorText}>{error}</span>
        </div>
      )}
    </form>
  );
}
