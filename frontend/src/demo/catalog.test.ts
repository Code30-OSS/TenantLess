import { describe, it, expect } from 'vitest';

import { CATALOG } from './catalog';
import { forbiddenTokenPattern } from '../test-utils/scrubTokens';

/**
 * DEMO-01 catalog data/contract (pure-module, no DOM — mirrors `api/queries.test.ts`).
 *
 * `CATALOG` is the "teach the contract" half of the scanner demo (D-01: orientation, not proof):
 * a curated, offline-safe list of the nine headline ARM discovery routes this simulator serves,
 * grouped into five capability sections. Each entry carries a method, route template, illustrative
 * api-version, purpose, `rootLabel` (the JsonTree payload root the S1 view displays), and a small
 * canned `sample` that illustrates the response *shape* (NOT fetched live). These assertions pin the
 * curated set so the S1 view (18-04) stays declarative and the OSS brand boundary holds (D-05).
 */

const GROUP_TITLES = [
  'DISCOVERY',
  'RESOURCE DETAIL',
  'COST MANAGEMENT',
  'AUTHORIZATION / RBAC',
  'IDENTITY / TOKEN',
] as const;

/** Expected entry count per group (interfaces table: Discovery=3, Detail=1, Cost=1, Auth=2, Identity=2). */
const EXPECTED_COUNTS: Record<(typeof GROUP_TITLES)[number], number> = {
  DISCOVERY: 3,
  'RESOURCE DETAIL': 1,
  'COST MANAGEMENT': 1,
  'AUTHORIZATION / RBAC': 2,
  'IDENTITY / TOKEN': 2,
};

const allEntries = () => CATALOG.flatMap((g) => g.entries);

describe('CATALOG — DEMO-01 endpoint catalog (D-01/D-01b)', () => {
  it('exposes exactly the five capability groups, in order', () => {
    expect(CATALOG.map((g) => g.title)).toEqual([...GROUP_TITLES]);
  });

  it('contains exactly 9 headline entries across the groups', () => {
    expect(allEntries()).toHaveLength(9);
  });

  it('has the expected per-group entry counts (3 / 1 / 1 / 2 / 2)', () => {
    for (const group of CATALOG) {
      expect(group.entries).toHaveLength(
        EXPECTED_COUNTS[group.title as (typeof GROUP_TITLES)[number]],
      );
    }
  });

  it('every entry has a valid method / route / purpose / rootLabel', () => {
    for (const entry of allEntries()) {
      expect(['GET', 'POST']).toContain(entry.method);
      expect(typeof entry.route).toBe('string');
      expect(entry.route.length).toBeGreaterThan(0);
      expect(entry.route.startsWith('/')).toBe(true);
      expect(typeof entry.purpose).toBe('string');
      expect(entry.purpose.length).toBeGreaterThan(0);
      expect(typeof entry.rootLabel).toBe('string');
      expect(entry.rootLabel.length).toBeGreaterThan(0);
    }
  });

  it('every entry.sample is a non-null, non-array object (MOCK-13 posture)', () => {
    for (const entry of allEntries()) {
      expect(typeof entry.sample).toBe('object');
      expect(entry.sample).not.toBeNull();
      expect(Array.isArray(entry.sample)).toBe(false);
    }
  });

  it('each illustrative api-version appears on at least one entry', () => {
    const versions = allEntries()
      .map((e) => e.apiVersion)
      .filter((v): v is string => typeof v === 'string');
    for (const expected of ['2022-12-01', '2021-04-01', '2025-03-01', '2022-04-01']) {
      expect(versions).toContain(expected);
    }
  });

  it('features no sim-only / console / control / drift / ui route (D-01b)', () => {
    const forbiddenRouteFragments = ['/_sim', '/_console', '/_control', '/simulator', '/ui'];
    for (const entry of allEntries()) {
      for (const fragment of forbiddenRouteFragments) {
        expect(entry.route.includes(fragment)).toBe(false);
      }
    }
  });

  it('contains no forbidden OSS brand token anywhere (D-05 / T-18-01)', () => {
    // Tokens come from tests/scrub-tokens.json plus the gitignored private
    // supplement -- never spelled in this source. They used to be assembled from
    // string fragments, which defeated the public/private split: deleting the
    // `+` signs reconstructed the private word list from a public file.
    const forbiddenBrand = forbiddenTokenPattern();
    expect(forbiddenBrand).not.toBeNull();
    expect(JSON.stringify(CATALOG)).not.toMatch(forbiddenBrand!);
  });
});
