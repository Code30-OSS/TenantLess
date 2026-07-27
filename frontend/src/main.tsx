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
 * D-04 — static tenant, NO polling: the QueryClient caches every fetched view indefinitely
 * (`staleTime: Infinity`) and disables every implicit refetch (focus / reconnect / interval).
 * A manual Refresh in the views is the only re-fetch path; the app never background-polls.
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
