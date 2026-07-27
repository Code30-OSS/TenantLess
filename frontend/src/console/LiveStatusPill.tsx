/**
 * LiveStatusPill (CONS-04) — the live/paused header control for the one deliberately-LIVE section in an
 * otherwise static SPA. Reflects the SSE lifecycle (`useEventStream` status) and the pause hold, plus a
 * `Pause`/`Resume` toggle button.
 *
 * States (UI-SPEC Copywriting / Motion):
 *  - **paused** → `❚❚ PAUSED`, dot steady `--text-2` (no blink); button reads `Resume`.
 *  - **live** (not paused) → `● LIVE`, a blinking green dot (the `blink` keyframe, `--green`); button
 *    reads `Pause`.
 *  - **connecting** → `connecting…` (`--text-2`).
 *  - **reconnecting** → `reconnecting…` (`--amber`) — the native `EventSource` auto-retry is in flight.
 *
 * Motion respects `prefers-reduced-motion` (global.css disables all animation). Only the status dot is
 * round (`50%`); everything else is sharp-cornered (D-01). Colors are `var(--token)` strings — no raw hex.
 */
import type { StreamStatus } from './useEventStream';
import styles from './LiveStatusPill.module.css';

interface LiveStatusPillProps {
  /** The SSE connection lifecycle (from `useEventStream`). */
  status: StreamStatus;
  /** Whether the live feed is currently paused (a hold — the socket stays open). */
  paused: boolean;
  /** Toggle pause/resume. */
  onToggle: () => void;
}

export default function LiveStatusPill({ status, paused, onToggle }: LiveStatusPillProps) {
  // Resolve the pill's dot color, blink state and label from (paused, status).
  let dotToken: string;
  let dotBlink = false;
  let text: string;
  let glyph: string | null = null;

  if (paused) {
    dotToken = '--text-2';
    glyph = '❚❚';
    text = 'PAUSED';
  } else if (status === 'live') {
    dotToken = '--green';
    dotBlink = true;
    glyph = '●';
    text = 'LIVE';
  } else if (status === 'reconnecting') {
    dotToken = '--amber';
    text = 'reconnecting…';
  } else {
    dotToken = '--text-2';
    text = 'connecting…';
  }

  return (
    <div className={styles.pill} role="status" aria-live="polite">
      <span
        className={dotBlink ? `${styles.dot} ${styles.blink}` : styles.dot}
        style={{ background: `var(${dotToken})` }}
        aria-hidden="true"
      />
      <span className={styles.text} style={{ color: `var(${dotToken})` }}>
        {glyph ? `${glyph} ${text}` : text}
      </span>
      <button type="button" className={styles.toggle} onClick={onToggle}>
        {paused ? 'Resume' : 'Pause'}
      </button>
    </div>
  );
}
