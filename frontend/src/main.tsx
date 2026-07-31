import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router';

import App from './App';
import './styles/tokens.css';
import './styles/global.css';

/**
 * Provider stack for the routed app (WEBUI-04, RESEARCH Pattern 4).
 *
 * D-04 — static tenant, no background polling under the ARM/Explorer QueryClient DEFAULTS below:
 * every fetched view is cached indefinitely (`staleTime: Infinity`) with every implicit refetch off
 * (focus / reconnect / interval), so a manual Refresh in the views is the manual re-fetch path.
 * The one deliberate exception is NOT a default: the app-level JobProvider (JobContext) runs an
 * ACTIVE-ONLY poll of the single in-flight control job (via `useJob`'s `refetchInterval`, which polls
 * only while `queued`/`running` and stops at a terminal status). When that job reaches `succeeded` it
 * fires a full `invalidateQueries()`, so the Explorer self-heals after a job completes — even if the
 * operator has navigated away from the control plane.
 *
 * D-06 — the SPA is embedded under `/ui`: BrowserRouter `basename="/ui"` mirrors Vite's
 * `base: '/ui/'` (15-01) so routing works identically in dev (proxy) and the embedded prod mount.
 * `fetch()` API paths stay absolute (never under `/ui`) so both environments agree (Pitfall 7).
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: Infinity,
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
      refetchInterval: false,
    },
  },
});

const rootEl = document.getElementById('root');
if (!rootEl) {
  throw new Error('Root element #root not found');
}

createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename="/ui">
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
