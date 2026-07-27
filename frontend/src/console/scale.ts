/**
 * Pure chart-scale math for the Observability Console latency chart (CONS-01). No fetch, no
 * React, no DOM — these functions only map data domains to SVG pixel ranges and turn a series
 * (with idle-bucket `null`s) into renderable polyline runs. Mirrors the pure `api/odata.ts`
 * idiom: small exported functions, edge-tolerant, never throwing. The thin SVG components
 * (LatencyChart, 16-05) consume these tested contracts so no component reinvents chart math.
 *
 * Design notes:
 * - `linScale` carries a zero-span guard (`span = dMax - dMin || 1`) so a flat domain (all-equal
 *   values, or a single bucket) never divides by zero — it collapses everything onto `rMin`.
 * - `polylinePoints` returns an ARRAY of run strings, one per contiguous non-null run, so a
 *   `null` (idle, no-traffic) bucket BREAKS the line into separate `<polyline>`s instead of
 *   dragging it to y=0 (RESEARCH Pitfall 5 — absence ≠ zero latency). The `x` mapper is called
 *   with the ORIGINAL series index, so a gap keeps its true horizontal width.
 * - `niceMax` rounds a series maximum up to a readable 1/2/5×10ⁿ axis ceiling and floors any
 *   non-positive / non-finite input to 1, so the y-axis is always a sane positive range.
 */

/** SVG viewBox width (CONS-01 UI-SPEC: `viewBox="0 0 720 180"`). */
export const VIEWBOX_W = 720;
/** SVG viewBox height (CONS-01 UI-SPEC: `viewBox="0 0 720 180"`). */
export const VIEWBOX_H = 180;
/** Inner-plot left inset — room for the y-axis ms labels. */
export const INSET_LEFT = 40;
/** Inner-plot bottom inset — room for the x-axis elapsed-seconds labels. */
export const INSET_BOTTOM = 20;
/** Inner-plot top inset. */
export const INSET_TOP = 8;
/** Inner-plot right inset. */
export const INSET_RIGHT = 8;

/**
 * Build a linear domain→range mapper. `linScale(dMin, dMax, rMin, rMax)(v)` maps `dMin→rMin`,
 * `dMax→rMax`, linearly in between (and extrapolates outside). A zero-span domain
 * (`dMin === dMax`) is guarded to `span = 1`, collapsing every input onto `rMin` rather than
 * producing `NaN`/`Infinity`. Supports an inverted range (`rMin > rMax`) for SVG's
 * downward-growing y axis.
 */
export function linScale(
  dMin: number,
  dMax: number,
  rMin: number,
  rMax: number,
): (v: number) => number {
  const span = dMax - dMin || 1;
  return (v: number) => rMin + ((v - dMin) / span) * (rMax - rMin);
}

/**
 * Turn a value series into one or more `<polyline>` point strings. Each contiguous run of
 * non-null values becomes one `"x,y x,y …"` string; a `null` value ends the current run so the
 * caller renders each run as a separate `<polyline>` — breaking the line across idle buckets
 * (Pitfall 5) instead of drawing down to zero. `x` receives the ORIGINAL series index (gaps keep
 * their width); `y` receives the value. Returns `[]` when every value is null.
 */
export function polylinePoints(
  values: readonly (number | null)[],
  x: (i: number) => number,
  y: (v: number) => number,
): string[] {
  const runs: string[] = [];
  let current: string[] = [];
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v === null) {
      if (current.length > 0) {
        runs.push(current.join(' '));
        current = [];
      }
      continue;
    }
    current.push(`${x(i)},${y(v)}`);
  }
  if (current.length > 0) runs.push(current.join(' '));
  return runs;
}

/**
 * Round a series maximum up to a readable axis ceiling — the nearest 1, 2, or 5 times a power of
 * ten (so a p95 max of 31 ms yields a `50` y-axis, 18 → 20, 4 → 5). Any non-positive or
 * non-finite input floors to `1`, guaranteeing a positive y range for `linScale`.
 */
export function niceMax(max: number): number {
  if (!Number.isFinite(max) || max <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(max)));
  const frac = max / mag;
  let nice: number;
  if (frac <= 1) nice = 1;
  else if (frac <= 2) nice = 2;
  else if (frac <= 5) nice = 5;
  else nice = 10;
  return nice * mag;
}
