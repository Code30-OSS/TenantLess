import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

/**
 * EXPL-05 — the server-side `$filter` CONTROLS. Post-Miller (15-16) the FilterBar renders NO result
 * list of its own (col2 {@link ResourceColumn} is the single place the RG's resources render); it
 * lifts the composed `$filter` up via `onApply`, which the view threads into col2. The filter string
 * is composed with the real `buildFilter` (15-01) — the tested odata.ts contract, NOT re-implemented.
 *
 * UAT Gap 6 (15-12): filters apply DELIBERATELY. Typing updates a DRAFT only; `onApply` fires only on
 * an explicit apply (Apply button / Enter / blur). No lift per keystroke.
 *
 * Pins:
 *  - typing a field does NOT call onApply (draft is not applied)
 *  - clicking Apply (or Enter / blur) lifts the composed `$filter`
 *  - a resourceType picker composes "resourceType eq '<v>'"; adding location joins with " and "
 *  - a raw `$filter` value passes through verbatim (power-user path), applied on Enter
 *  - Clear lifts `undefined` (returns col2 to the unfiltered listing) and empties the drafts
 *  - no result list / pager is rendered here (that lives in col2 now — no duplication)
 */

import FilterBar from './FilterBar';

const STORAGE = 'Microsoft.Storage/storageAccounts';

function renderBar() {
  const onApply = vi.fn();
  render(<FilterBar sub="sub-a" rg="rg-app" onApply={onApply} />);
  return { onApply };
}

/** The last value passed to onApply. */
function lastApplied(onApply: ReturnType<typeof vi.fn>) {
  const calls = onApply.mock.calls;
  return calls.length ? (calls[calls.length - 1][0] as string | undefined) : undefined;
}

function apply() {
  fireEvent.click(screen.getByRole('button', { name: /apply filter/i }));
}

describe('FilterBar — deliberate apply (UAT Gap 6)', () => {
  it('typing a field does NOT lift a filter (draft only, no per-keystroke apply)', () => {
    const { onApply } = renderBar();
    fireEvent.change(screen.getByLabelText('resourceType'), { target: { value: 'Micro' } });

    expect(onApply).not.toHaveBeenCalled();
  });

  it('applying lifts the composed $filter', () => {
    const { onApply } = renderBar();
    fireEvent.change(screen.getByLabelText('resourceType'), { target: { value: STORAGE } });
    // still not applied — the pending draft has not lifted
    expect(onApply).not.toHaveBeenCalled();

    apply();
    expect(lastApplied(onApply)).toBe(`resourceType eq '${STORAGE}'`);
  });
});

describe('FilterBar — guided compose (applied)', () => {
  it('composes "resourceType eq \'<v>\'" from the resourceType picker on apply', () => {
    const { onApply } = renderBar();
    fireEvent.change(screen.getByLabelText('resourceType'), { target: { value: STORAGE } });
    apply();

    expect(onApply).toHaveBeenLastCalledWith(`resourceType eq '${STORAGE}'`);
  });

  it('joins a location clause with " and " on apply', () => {
    const { onApply } = renderBar();
    fireEvent.change(screen.getByLabelText('resourceType'), { target: { value: STORAGE } });
    fireEvent.change(screen.getByLabelText('location'), { target: { value: 'westeurope' } });
    apply();

    expect(onApply).toHaveBeenLastCalledWith(
      `resourceType eq '${STORAGE}' and location eq 'westeurope'`,
    );
  });

  it('applies on field blur as well (blur affordance)', () => {
    const { onApply } = renderBar();
    const rt = screen.getByLabelText('resourceType');
    fireEvent.change(rt, { target: { value: STORAGE } });
    fireEvent.blur(rt);

    expect(lastApplied(onApply)).toBe(`resourceType eq '${STORAGE}'`);
  });
});

describe('FilterBar — raw passthrough', () => {
  it('passes a raw $filter value straight through on Enter', () => {
    const { onApply } = renderBar();
    const raw = screen.getByLabelText('$filter');
    fireEvent.change(raw, { target: { value: "tagName eq 'env'" } });
    // not yet applied
    expect(onApply).not.toHaveBeenCalled();

    fireEvent.keyDown(raw, { key: 'Enter' });
    expect(onApply).toHaveBeenLastCalledWith("tagName eq 'env'");
  });
});

describe('FilterBar — clear filter', () => {
  it('clears the composed filter (lifts undefined) and empties the draft inputs', () => {
    const { onApply } = renderBar();
    fireEvent.change(screen.getByLabelText('resourceType'), { target: { value: STORAGE } });
    apply();
    expect(lastApplied(onApply)).toBe(`resourceType eq '${STORAGE}'`);

    fireEvent.click(screen.getByRole('button', { name: /clear filter/i }));

    expect(lastApplied(onApply)).toBeUndefined();
    expect((screen.getByLabelText('resourceType') as HTMLInputElement).value).toBe('');
  });
});

describe('FilterBar — controls only (no duplicate list post-Miller)', () => {
  it('renders no result list or pager of its own (col2 owns the resource list)', () => {
    renderBar();
    // The old full-width results list + range readout are gone — col2 is the single list surface.
    expect(screen.queryByTestId('range-readout')).toBeNull();
    expect(screen.queryByRole('button', { name: /^next/i })).toBeNull();
  });
});
