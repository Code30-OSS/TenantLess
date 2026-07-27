import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { ArmError } from './api/client';
import App from './App';

/**
 * The `/control-plane` route now mounts the real `ControlPlaneView` (17-06), which probes
 * `GET /_control/probe` on mount. The probe is mocked to a 404 so the view resolves deterministically
 * to the "disarmed" explainer card — proving the ComingSoon PHASE-17 stub is gone WITHOUT a network.
 * (No other route in this file imports `../api/control`, so this mock is scoped to the control plane.)
 */
vi.mock('./api/control', () => ({
  controlGet: () => Promise.reject(new ArmError('NotFound', 'not found', 404)),
  setControlToken: () => {},
  useJob: () => ({ data: undefined }),
}));

/**
 * Routed-shell integration test (WEBUI-04 — the routing half).
 *
 * Renders the real `<App/>` route table under a `MemoryRouter basename="/ui"` (routing exercised,
 * not mocked) wrapped in a QueryClientProvider (so any view a route mounts can use react-query).
 * The shell-COMPONENT unit assertions (nav groups, ComingSoon copy in isolation, theme flip) live
 * in 15-07's shell.test.tsx; here we prove the five-route table + index redirect over that shell.
 *
 * initialEntries carry the `/ui` prefix — `basename` strips it before matching (Pitfall 7: the
 * app must route correctly under the embed base, not only at `/`).
 */
function renderAppAt(path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter basename="/ui" initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('App route table — stub sections (ComingSoon copy + phase tag)', () => {
  it('/ui/console renders the real Observability Console view (16-07 route swap, not the stub)', () => {
    renderAppAt('/ui/console');
    // The console route now mounts <ConsoleView/> (Phase 16), replacing the ComingSoon PHASE 16 stub.
    expect(screen.getByRole('heading', { name: 'Observability Console' })).toBeTruthy();
    expect(screen.queryByText('PHASE 16')).toBeNull();
  });

  it('/ui/control-plane redirects to /generate and mounts ControlPlaneView (PHASE 17 stub removed)', async () => {
    renderAppAt('/ui/control-plane');
    // The probe (mocked 404) resolves the view to the disarmed explainer card; the ComingSoon stub is gone.
    expect(await screen.findByRole('heading', { name: 'Control plane not enabled' })).toBeTruthy();
    expect(screen.queryByText('PHASE 17')).toBeNull();
  });

  it('/ui/demo redirects to the catalog and mounts CatalogView (PHASE 18 stub removed)', () => {
    renderAppAt('/ui/demo');
    // The demo route now redirects to /demo/catalog → <CatalogView/> (Phase 18), replacing the stub.
    expect(screen.getByRole('heading', { name: 'What an ARM scanner sees' })).toBeTruthy();
    expect(screen.queryByText('PHASE 18')).toBeNull();
  });

  it('/ui/overview mounts the OverviewView front door (no PHASE 18 stub)', () => {
    renderAppAt('/ui/overview');
    expect(screen.getByRole('heading', { name: 'Point a scanner at this tenant.' })).toBeTruthy();
    expect(screen.queryByText('PHASE 18')).toBeNull();
  });
});

describe('App route table — index redirect', () => {
  it('/ui/ redirects to the overview front door (D-04 index re-point)', () => {
    renderAppAt('/ui/');
    expect(screen.getByRole('heading', { name: 'Point a scanner at this tenant.' })).toBeTruthy();
  });

  it('/ui/explorer/dependencies mounts the DependenciesView placeholder', () => {
    renderAppAt('/ui/explorer/dependencies');
    expect(screen.getByTestId('dependencies-view')).toBeTruthy();
  });
});

describe('App route table — shell + nav active state over the route table', () => {
  it('renders the five section groups of the shared AppShell SidebarNav', () => {
    const { container } = renderAppAt('/ui/explorer/resources');
    // Overview group prepended + Explorer/Console/Control Plane/Demo (append-only, 18-07).
    expect(container.querySelectorAll('[data-nav-group]')).toHaveLength(5);
  });

  it('marks the Resources nav item active on /ui/explorer/resources (gold via aria-current)', () => {
    renderAppAt('/ui/explorer/resources');
    const active = screen.getByRole('link', { name: 'Resources' });
    const idle = screen.getByRole('link', { name: 'Dependencies' });
    expect(active.getAttribute('aria-current')).toBe('page');
    expect(idle.getAttribute('aria-current')).toBeNull();
  });

  it('marks the Console nav item active on /ui/console', () => {
    renderAppAt('/ui/console');
    const active = screen.getByRole('link', { name: 'Console' });
    const idle = screen.getByRole('link', { name: 'Resources' });
    expect(active.getAttribute('aria-current')).toBe('page');
    expect(idle.getAttribute('aria-current')).toBeNull();
  });
});
