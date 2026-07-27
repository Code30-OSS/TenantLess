import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import AppShell from './AppShell';
import SidebarNav from './SidebarNav';
import ComingSoon from '../common/ComingSoon';

/**
 * Routing-INDEPENDENT shell contract (WEBUI-04). The routed-shell integration (index redirect,
 * stub routes over the real route table) is asserted separately in 15-03's App.test.tsx.
 */

function renderSidebarAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <SidebarNav />
    </MemoryRouter>,
  );
}

function renderShellAt(path: string) {
  // AppShell mounts Topbar, which now reads the shared summary cache via useSummary (UAT Gap 4) —
  // so the shell render needs a QueryClient in context. The query itself is irrelevant to the
  // theme-flip assertion; Topbar guards an undefined summary.
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="explorer/resources" element={<div>resources view</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ComingSoon (shared stub — Placeholder Contract copy)', () => {
  it('renders the console section heading and phase tag', () => {
    render(<ComingSoon section="console" />);
    expect(screen.getByRole('heading', { name: 'Observability Console' })).toBeTruthy();
    expect(screen.getByText('PHASE 16')).toBeTruthy();
  });

  it('renders the control-plane section heading and phase tag', () => {
    render(<ComingSoon section="control-plane" />);
    expect(screen.getByRole('heading', { name: 'Control Plane' })).toBeTruthy();
    expect(screen.getByText('PHASE 17')).toBeTruthy();
  });

  it('renders the demo section heading and phase tag', () => {
    render(<ComingSoon section="demo" />);
    expect(screen.getByRole('heading', { name: 'Scanner Demo' })).toBeTruthy();
    expect(screen.getByText('PHASE 18')).toBeTruthy();
  });
});

describe('SidebarNav (data-driven five-section nav)', () => {
  it('renders exactly five section groups', () => {
    const { container } = renderSidebarAt('/explorer/resources');
    // Overview group prepended + Explorer/Console/Control Plane/Demo (append-only, 18-07).
    expect(container.querySelectorAll('[data-nav-group]')).toHaveLength(5);
  });

  it('renders a navigable link for each section (Explorer has two, Control Plane four, Demo three — 18-07)', () => {
    renderSidebarAt('/explorer/resources');
    expect(screen.getByRole('link', { name: 'Overview' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Resources' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Dependencies' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Console' })).toBeTruthy();
    // The Control Plane group expanded to the four control-plane sections (append-only, D-16).
    expect(screen.getByRole('link', { name: 'generate' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'analyze' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'tenants' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'server status' })).toBeTruthy();
    // The Demo group expanded to the three demo sub-pages (append-only, 18-07).
    expect(screen.getByRole('link', { name: 'catalog' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'scanner' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'viewer' })).toBeTruthy();
  });

  it('marks the item matching the current location as active (gold state via aria-current)', () => {
    renderSidebarAt('/explorer/resources');
    const active = screen.getByRole('link', { name: 'Resources' });
    const idle = screen.getByRole('link', { name: 'Dependencies' });
    expect(active.getAttribute('aria-current')).toBe('page');
    expect(idle.getAttribute('aria-current')).toBeNull();
  });
});

describe('ThemeToggle ([data-theme] flip)', () => {
  it('flips the shell wrapper [data-theme] from dark to light on click', () => {
    const { container } = renderShellAt('/explorer/resources');
    const wrapper = container.querySelector('[data-theme]');
    expect(wrapper).not.toBeNull();
    expect(wrapper!.getAttribute('data-theme')).toBe('dark');

    fireEvent.click(screen.getByRole('button', { name: /switch to light theme/i }));
    expect(wrapper!.getAttribute('data-theme')).toBe('light');

    fireEvent.click(screen.getByRole('button', { name: /switch to dark theme/i }));
    expect(wrapper!.getAttribute('data-theme')).toBe('dark');
  });
});

describe('tokens.css light palette matches the shell wrapper (not :root)', () => {
  // AppShell stamps [data-theme] on a wrapper <div> (proven by the flip test above), NOT on
  // the <html> root. A `:root[data-theme="light"]` selector therefore never matches the wrapper,
  // so toggling to light does nothing (the unconditional `:root` dark defaults keep applying).
  // The light override MUST be keyed on the bare attribute selector so it matches the wrapper.
  // Vitest runs with cwd at the frontend root, so resolve the token file from there.
  // Strip /* … */ comments so the assertions check real selectors, not explanatory prose.
  const tokensCss = readFileSync(resolve(process.cwd(), 'src/styles/tokens.css'), 'utf8').replace(
    /\/\*[\s\S]*?\*\//g,
    '',
  );

  it('defines a light-theme override', () => {
    expect(tokensCss).toMatch(/\[data-theme="light"\]\s*\{/);
  });

  it('does NOT scope the light-theme override to :root (which cannot match the wrapper div)', () => {
    expect(tokensCss).not.toMatch(/:root\s*\[data-theme="light"\]/);
  });
});
