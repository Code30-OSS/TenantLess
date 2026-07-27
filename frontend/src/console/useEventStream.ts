/**
 * The Console live-feed hook (CONS-04, RESEARCH Pattern 4) — the one deliberately LIVE data path in an
 * otherwise static, no-poll SPA. Subscribes to the bearer-exempt `/_console/stream` SSE endpoint via
 * the native browser {@link EventSource} and folds each pushed `RequestEvent` into a bounded,
 * newest-first buffer.
 *
 * Deliberately NOT a TanStack Query hook: the QueryClient's `staleTime: Infinity` / no-refetch config
 * (`main.tsx`) models a static tenant snapshot, which is the opposite of a push stream. The precedent
 * for the raw `EventSource` call is the legacy `console.html` page (proven through the Vite `/_console`
 * proxy). Native `EventSource` also gives us auto-reconnect for free — no custom backoff (Pattern 4).
 *
 * Three invariants worth stating (T-16-05 — client DoS):
 *  - **Bounded buffer:** every append is `[ev, ...prev].slice(0, cap)`, so memory is capped at `cap`
 *    regardless of stream rate (a burst can't grow the array without bound).
 *  - **Leak-safe:** the socket is `.close()`d in the `useEffect` cleanup — a remount/unmount never
 *    leaves an orphaned connection open.
 *  - **Pause is a HOLD, not a close:** while paused we drop incoming events from view but keep the
 *    socket open (via a `useRef` flag read inside `onmessage`), so resuming is instant and we don't
 *    thrash the connection.
 *
 * The buffer is seeded ONCE from `seed` (the newest-first `/_console/stats.recent[]` slice) on mount,
 * then SSE prepends — there is no repeated full-`recent` poll (RESEARCH Pitfall 3). The scanner-
 * controlled `path`/`route` strings are only `JSON.parse`d and buffered here; XSS-safe rendering is
 * the feed/inspector's job (T-16-03, deferred to the JSX consumers).
 */
import { useEffect, useRef, useState } from 'react';

import type { RequestEvent } from '../api/console';

/** Live-connection state surfaced to the feed header. `reconnecting` = native auto-retry in flight. */
export type StreamStatus = 'connecting' | 'live' | 'reconnecting';

export interface UseEventStream {
  /** Newest-first buffer, bounded to `cap`. */
  events: RequestEvent[];
  status: StreamStatus;
  /** Toggle the hold: `pause(true)` drops incoming events from view but keeps the socket open. */
  pause: (paused: boolean) => void;
}

/**
 * Stream `/_console/stream` into a bounded, newest-first buffer with a pause hold.
 *
 * @param cap  Maximum buffered events (match the server ring depth so client-side window filters have
 *             the same depth). Also re-opens the socket if it changes.
 * @param seed Optional one-shot seed (newest-first `stats.recent[]`) — applied on mount only.
 */
export function useEventStream(cap: number, seed?: RequestEvent[]): UseEventStream {
  // Seed once on first mount; `slice(0, cap)` keeps even the seed within bounds.
  const [events, setEvents] = useState<RequestEvent[]>(() => (seed ? seed.slice(0, cap) : []));
  const [status, setStatus] = useState<StreamStatus>('connecting');
  const pausedRef = useRef(false);

  useEffect(() => {
    // Root-relative, same-origin path (Vite proxy in dev / same-origin `/ui` embed in prod). Never an
    // absolute origin — no cross-origin SSE.
    const es = new EventSource('/_console/stream');

    es.onopen = () => setStatus('live');
    es.onerror = () => setStatus('reconnecting'); // EventSource auto-reconnects natively.
    es.onmessage = (e: MessageEvent) => {
      if (pausedRef.current) return; // paused = hold; keep the socket open, drop from view.
      const event = JSON.parse(e.data) as RequestEvent;
      setEvents((prev) => [event, ...prev].slice(0, cap)); // prepend newest-first, bounded.
    };

    return () => es.close(); // leak-safe: always close on unmount / cap change (T-16-05).
  }, [cap]);

  const pause = (paused: boolean): void => {
    pausedRef.current = paused;
  };

  return { events, status, pause };
}
