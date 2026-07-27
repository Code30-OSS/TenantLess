import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

/**
 * CatalogView (DEMO-01, 18-UI-SPEC §S1) — the S1 endpoint catalog.
 *
 * The view is static (no router / no query client needed): it renders the 18-01 `CATALOG` data as
 * grouped headline ARM routes. These tests pin the S1 contract:
 *  - the H1 + all five capability group headings render;
 *  - both a GET and a POST method chip appear;
 *  - the `Sample response` disclosure LAZILY mounts the reused JsonTree — the `role="tree"` node is
 *    absent before expanding and present after clicking the disclosure (D-01/D-01a lazy mount).
 */

import CatalogView from './CatalogView';

const GROUP_HEADINGS = [
  'DISCOVERY',
  'RESOURCE DETAIL',
  'COST MANAGEMENT',
  'AUTHORIZATION / RBAC',
  'IDENTITY / TOKEN',
];

describe('CatalogView — DEMO-01 endpoint catalog', () => {
  it('renders the H1 and all five capability group headings', () => {
    render(<CatalogView />);
    expect(screen.getByRole('heading', { name: 'What an ARM scanner sees' })).toBeTruthy();
    for (const heading of GROUP_HEADINGS) {
      expect(screen.getByText(heading)).toBeTruthy();
    }
  });

  it('renders at least one GET method chip and one POST method chip', () => {
    render(<CatalogView />);
    expect(screen.getAllByText('GET').length).toBeGreaterThan(0);
    expect(screen.getAllByText('POST').length).toBeGreaterThan(0);
  });

  it('lazily mounts the JsonTree only after the Sample response disclosure is expanded', () => {
    render(<CatalogView />);

    // Before any disclosure is expanded, no JsonTree (role="tree") is in the DOM.
    expect(screen.queryByRole('tree')).toBeNull();

    const [firstDisclosure] = screen.getAllByRole('button', { name: /sample response/i });
    expect(firstDisclosure.getAttribute('aria-expanded')).toBe('false');

    fireEvent.click(firstDisclosure);

    // After expanding, the reused JsonTree mounts and exposes its role="tree" node.
    expect(firstDisclosure.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getAllByRole('tree').length).toBeGreaterThan(0);
  });

  it('renders the curated sample fully expanded so nested content is visible without further clicks', () => {
    // UAT gap (Ph18): the sample tree was double-wrapped ({value:{value:[…]}}) and collapsed by
    // default, so an expanded disclosure read as empty. The curated samples are small and exist to
    // teach the response shape, so their content must be visible on expand — here, a leaf value nested
    // two levels deep inside the first /subscriptions list sample.
    render(<CatalogView />);

    const [firstDisclosure] = screen.getAllByRole('button', { name: /sample response/i });
    fireEvent.click(firstDisclosure);

    // A deeply-nested leaf value from CATALOG[0].entries[0].sample.value[0] must be on screen.
    expect(screen.getByText(/Platform Production/)).toBeTruthy();
    // And the redundant double-`value` wrapper must be gone: exactly one `value` key node renders
    // (the envelope's own array), not an outer `value {` wrapping an inner `value [`.
    expect(screen.getAllByText('value')).toHaveLength(1);
  });
});
