import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';

import JsonTree from './JsonTree';

/**
 * EXPL-02 / D-03 — the hand-rolled collapsible JSON tree that hosts arbitrary-depth ARM `properties`.
 * Pins: renders every depth, per-node expand/collapse hides children, typed leaves, copy-path /
 * copy-value keyed by the node's dotted path, and the empty `properties:{}` → "No properties" note
 * (never crashes on an empty object — MOCK-13 guarantees an object, never null).
 */

const writeText = vi.fn();

beforeEach(() => {
  writeText.mockReset();
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
  });
});

describe('JsonTree — depth + expand/collapse', () => {
  it('reaches every depth of a nested object/array by expanding branch-by-branch', () => {
    render(<JsonTree data={{ a: { b: { c: 1 } }, list: [1, { x: 2 }] }} />);

    // Root is open by default; its immediate branches are visible but collapsed.
    expect(screen.getByLabelText('Toggle properties')).toBeTruthy();
    expect(screen.getByLabelText('Toggle a')).toBeTruthy();
    expect(screen.getByLabelText('Toggle list')).toBeTruthy();
    // A deeper branch (depth >= 2) is NOT mounted until its parent is expanded.
    expect(screen.queryByLabelText('Toggle b')).toBeNull();

    // Expand `a` → its immediate child branch `b` mounts (itself collapsed).
    fireEvent.click(screen.getByLabelText('Toggle a'));
    expect(screen.getByLabelText('Toggle b')).toBeTruthy();
    expect(screen.queryByLabelText('Copy path properties.a.b.c')).toBeNull();

    // Expand `b` → the deepest leaf is now reachable.
    fireEvent.click(screen.getByLabelText('Toggle b'));
    expect(screen.getByLabelText('Copy path properties.a.b.c')).toBeTruthy();
  });

  it('collapses a branch on toggle so its children are hidden, and re-expands on a second click', () => {
    render(<JsonTree data={{ a: { b: { c: 1 } } }} />);

    // `a` starts collapsed (depth >= 1) — expand it to reveal `b`.
    fireEvent.click(screen.getByLabelText('Toggle a'));
    expect(screen.getByLabelText('Toggle b')).toBeTruthy();

    // Collapse `a` again — `b` unmounts.
    fireEvent.click(screen.getByLabelText('Toggle a'));
    expect(screen.queryByLabelText('Toggle b')).toBeNull();

    // Re-expand — `b` comes back.
    fireEvent.click(screen.getByLabelText('Toggle a'));
    expect(screen.getByLabelText('Toggle b')).toBeTruthy();
  });
});

describe('JsonTree — collapsed by default beyond a shallow depth (D-03, UAT Gap 3)', () => {
  it('branches below the shallow threshold default collapsed — a depth>=2 leaf is not in the DOM on first paint', () => {
    render(<JsonTree data={{ a: { b: 1 } }} />);

    // Root's immediate keys are visible…
    expect(screen.getByLabelText('Toggle properties')).toBeTruthy();
    expect(screen.getByLabelText('Toggle a')).toBeTruthy();
    // …but `a` (depth 1) is collapsed, so its leaf `b` (depth 2) never mounted.
    expect(screen.queryByLabelText('Copy path properties.a.b')).toBeNull();
  });

  it('expanding a collapsed branch mounts only its immediate children, not the whole subtree', () => {
    render(<JsonTree data={{ a: { b: { c: 1 } } }} />);

    fireEvent.click(screen.getByLabelText('Toggle a'));
    // `a`'s direct child branch `b` appears…
    expect(screen.getByLabelText('Toggle b')).toBeTruthy();
    // …but its grandchild leaf `c` is still absent (`b` is itself collapsed).
    expect(screen.queryByLabelText('Copy path properties.a.b.c')).toBeNull();
  });

  it('renders a pathologically deep (~200-level) blob without throwing or eagerly recursing', () => {
    let o: Record<string, unknown> = { leaf: 1 };
    for (let i = 0; i < 200; i++) o = { nested: o };

    let container: HTMLElement | undefined;
    expect(() => {
      container = render(<JsonTree data={o} />).container;
    }).not.toThrow();

    // Shallow structure mounts…
    expect(screen.getByLabelText('Toggle properties')).toBeTruthy();
    expect(screen.getByLabelText('Toggle nested')).toBeTruthy();
    // …but the mount is bounded: the deep leaf did not render eagerly.
    expect(screen.queryByText('1')).toBeNull();
    expect(container).toBeTruthy();
  });
});

describe('JsonTree — initialOpenDepth + hideRoot (S1 demo catalog, Ph18 UAT gap)', () => {
  it('initialOpenDepth expands every branch by default so a deep leaf mounts without clicks', () => {
    render(
      <JsonTree data={{ a: { b: { c: 1 } } }} initialOpenDepth={Number.POSITIVE_INFINITY} />,
    );
    // With a large initialOpenDepth nothing starts collapsed — the depth-3 leaf is on first paint.
    expect(screen.getByLabelText('Copy path properties.a.b.c')).toBeTruthy();
  });

  it('hideRoot renders the envelope entries directly — no redundant root wrapper node', () => {
    render(
      <JsonTree
        data={{ value: [{ id: 'x' }] }}
        rootLabel="value"
        hideRoot
        initialOpenDepth={Number.POSITIVE_INFINITY}
      />,
    );
    // Exactly one `value` node (the envelope's own key), not an outer `value {` around an inner `value [`.
    expect(screen.getByLabelText('Toggle value')).toBeTruthy();
    expect(screen.queryByLabelText('Toggle properties')).toBeNull();
    // The nested leaf inside the array is reachable without further clicks (fully expanded).
    expect(screen.getByText('"x"')).toBeTruthy();
  });

  it('hideRoot falls back to a single root node when data is not a non-empty branch', () => {
    render(<JsonTree data={{}} hideRoot />);
    // An empty object has no entries to hoist, so the wrapper/"No properties" note still renders.
    expect(screen.getByText('No properties')).toBeTruthy();
  });
});

describe('JsonTree — typed leaves', () => {
  it('renders string (quoted), number, boolean and null as distinct typed values', () => {
    const { container } = render(<JsonTree data={{ s: 'hi', n: 42, b: true, z: null }} />);

    const str = container.querySelector('[data-type="string"]');
    const num = container.querySelector('[data-type="number"]');
    const bool = container.querySelector('[data-type="boolean"]');
    const nul = container.querySelector('[data-type="null"]');

    expect(str?.textContent).toBe('"hi"');
    expect(num?.textContent).toBe('42');
    expect(bool?.textContent).toBe('true');
    expect(nul?.textContent).toBe('null');
  });
});

describe('JsonTree — copy path / copy value keyed by node path', () => {
  it('copy-path writes the dotted path of the node', () => {
    render(<JsonTree data={{ a: { b: { c: 1 } } }} />);
    // Deep branches start collapsed — expand down to the leaf first.
    fireEvent.click(screen.getByLabelText('Toggle a'));
    fireEvent.click(screen.getByLabelText('Toggle b'));
    fireEvent.click(screen.getByLabelText('Copy path properties.a.b.c'));
    expect(writeText).toHaveBeenCalledWith('properties.a.b.c');
  });

  it('copy-value writes JSON.stringify(value) (string stays quoted, number bare)', () => {
    render(<JsonTree data={{ s: 'hi', n: 42 }} />);

    fireEvent.click(screen.getByLabelText('Copy value properties.n'));
    expect(writeText).toHaveBeenCalledWith('42');

    fireEvent.click(screen.getByLabelText('Copy value properties.s'));
    expect(writeText).toHaveBeenCalledWith('"hi"');
  });
});

describe('JsonTree — empty properties', () => {
  it('renders an empty object with a "No properties" note and does not crash', () => {
    const { container } = render(<JsonTree data={{}} />);
    expect(within(container).getByText('No properties')).toBeTruthy();
  });
});
