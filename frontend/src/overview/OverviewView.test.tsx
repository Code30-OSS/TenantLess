import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';

import OverviewView from './OverviewView';

/**
 * OverviewView (S0 — DEMO front door) unit test.
 *
 * Rendered under `MemoryRouter basename="/ui"` (matching the embed base) so the four orientation-card
 * `<Link>`s resolve their `href`s the way they will in the real SPA. Asserts the verbatim S0 H1 and
 * that each card links to its target section (Explorer / Console / Control Plane / Scanner Demo).
 */
function renderOverview() {
  return render(
    <MemoryRouter basename="/ui" initialEntries={['/ui/overview']}>
      <OverviewView />
    </MemoryRouter>,
  );
}

describe('OverviewView (S0 — landing front door)', () => {
  it('renders the verbatim S0 head-band H1', () => {
    renderOverview();
    expect(
      screen.getByRole('heading', { name: 'Point a scanner at this tenant.' }),
    ).toBeTruthy();
  });

  it('renders four orientation-card links resolving to their targets (basename /ui)', () => {
    renderOverview();
    const cases: [RegExp, string][] = [
      [/Explorer/, '/ui/explorer/resources'],
      [/Console/, '/ui/console'],
      [/Control Plane/, '/ui/control-plane/generate'],
      [/Scanner Demo/, '/ui/demo/catalog'],
    ];
    for (const [name, href] of cases) {
      expect(screen.getByRole('link', { name }).getAttribute('href')).toBe(href);
    }
  });
});
