import { describe, it, expect } from 'vitest';

import {
  linScale,
  polylinePoints,
  niceMax,
  VIEWBOX_W,
  VIEWBOX_H,
  INSET_LEFT,
  INSET_BOTTOM,
  INSET_TOP,
  INSET_RIGHT,
} from './scale';

describe('linScale', () => {
  it('maps dMin → rMin and dMax → rMax', () => {
    const s = linScale(0, 10, 0, 100);
    expect(s(0)).toBe(0);
    expect(s(10)).toBe(100);
  });

  it('maps the midpoint of the domain to the midpoint of the range', () => {
    expect(linScale(0, 10, 0, 100)(5)).toBe(50);
  });

  it('supports an inverted range (SVG y grows downward: rMin > rMax)', () => {
    const y = linScale(0, 10, 180, 0);
    expect(y(0)).toBe(180);
    expect(y(10)).toBe(0);
    expect(y(5)).toBe(90);
  });

  it('never divides by zero when dMin === dMax (zero-span guard)', () => {
    const s = linScale(5, 5, 0, 100);
    const out = s(5);
    expect(Number.isFinite(out)).toBe(true);
    expect(out).toBe(0); // (5-5)/1 * range + rMin
  });
});

describe('polylinePoints', () => {
  it('returns a single run of "x,y" pairs when no value is null', () => {
    expect(
      polylinePoints(
        [1, 2, 3],
        (i) => i,
        (v) => v,
      ),
    ).toEqual(['0,1 1,2 2,3']);
  });

  it('splits into TWO runs when an interior null breaks the line (Pitfall 5)', () => {
    expect(
      polylinePoints(
        [1, null, 3],
        (i) => i,
        (v) => v,
      ),
    ).toEqual(['0,1', '2,3']);
  });

  it('returns an empty array when every value is null (all-idle)', () => {
    expect(
      polylinePoints(
        [null, null, null],
        (i) => i,
        (v) => v,
      ),
    ).toEqual([]);
  });

  it('ignores leading and trailing nulls, keeping the interior run', () => {
    expect(
      polylinePoints(
        [null, 2, 3, null],
        (i) => i,
        (v) => v,
      ),
    ).toEqual(['1,2 2,3']);
  });

  it('uses the ORIGINAL index for x so the gap width is preserved', () => {
    expect(
      polylinePoints(
        [5, null, 7],
        (i) => i * 10,
        (v) => v,
      ),
    ).toEqual(['0,5', '20,7']);
  });
});

describe('niceMax', () => {
  it('floors non-positive input to a sane axis ceiling of 1', () => {
    expect(niceMax(0)).toBe(1);
    expect(niceMax(-5)).toBe(1);
  });

  it('rounds representative latency maxima up to a readable 1/2/5×10ⁿ ceiling', () => {
    expect(niceMax(4)).toBe(5);
    expect(niceMax(7)).toBe(10);
    expect(niceMax(10)).toBe(10);
    expect(niceMax(18)).toBe(20);
    expect(niceMax(31)).toBe(50);
    expect(niceMax(200)).toBe(200);
  });
});

describe('chart dimension constants (CONS-01 SVG spec)', () => {
  it('locks the viewBox + inset geometry to the UI-SPEC', () => {
    expect(VIEWBOX_W).toBe(720);
    expect(VIEWBOX_H).toBe(180);
    expect(INSET_LEFT).toBe(40);
    expect(INSET_BOTTOM).toBe(20);
    expect(INSET_TOP).toBe(8);
    expect(INSET_RIGHT).toBe(8);
  });
});
