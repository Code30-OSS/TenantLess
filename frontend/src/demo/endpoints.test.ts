import { describe, it, expect } from 'vitest';

import { RUNNABLE_ENDPOINTS } from './endpoints';
import { resourceGroupsUrl, resourcesUrl } from '../api/queries';

/**
 * DEMO-03 contract: the runnable-endpoint MODEL the live viewer (18-06) consumes.
 *
 * RUNNABLE_ENDPOINTS is a pure, DOM-free table describing which headline ARM routes the S3 viewer
 * can actually execute (GET only, resolves against a live tenant), whether each needs a real
 * subscription id and/or a real resource id, and how to COMPOSE the concrete same-origin ARM path
 * from those ids. Path composition REUSES the exported, unit-tested `queries.ts` builders
 * (`resourceGroupsUrl`/`resourcesUrl`, per-segment `encodeURIComponent`, WR-02) — never string
 * concat, never a new fetch. Cost (POST) and token/JWKS are catalog-only, NOT runnable (D-03).
 */

// A URL-unsafe subscription value: the `/` would inject an extra path segment if not encoded (WR-02).
const UNSAFE_SUB = 'sub/with space';
const SAMPLE_SUB = 'sub-a';
const SAMPLE_RG = 'rg-1';
const SAMPLE_ARM_ID =
  '/subscriptions/sub-a/resourceGroups/rg-1/providers/Microsoft.Storage/storageAccounts/acct1';

function byId(id: string) {
  const ep = RUNNABLE_ENDPOINTS.find((e) => e.id === id);
  if (!ep) throw new Error(`no runnable endpoint with id=${id}`);
  return ep;
}

describe('RUNNABLE_ENDPOINTS — DEMO-03 runnable-endpoint model', () => {
  it('lists exactly the 5 runnable GET endpoints (D-03)', () => {
    expect(RUNNABLE_ENDPOINTS.map((e) => e.id).sort()).toEqual(
      ['resource-detail', 'resource-groups', 'resources', 'role-assignments', 'subscriptions'].sort(),
    );
  });

  it('every runnable endpoint is a GET (no POST/mutation, T-18-07)', () => {
    for (const ep of RUNNABLE_ENDPOINTS) {
      expect(ep.method).toBe('GET');
    }
  });

  it('models no /_sim, /_console, or /_control route (D-03b)', () => {
    for (const ep of RUNNABLE_ENDPOINTS) {
      const path = ep.build({ sub: SAMPLE_SUB, rg: SAMPLE_RG, armId: SAMPLE_ARM_ID });
      expect(path).not.toMatch(/\/_sim|\/_console|\/_control/);
    }
  });

  it('each endpoint declares a stable id, a label, and needsSubId/needsResId flags', () => {
    for (const ep of RUNNABLE_ENDPOINTS) {
      expect(typeof ep.id).toBe('string');
      expect(ep.id.length).toBeGreaterThan(0);
      expect(typeof ep.label).toBe('string');
      expect(ep.label.length).toBeGreaterThan(0);
      expect(typeof ep.needsSubId).toBe('boolean');
      expect(typeof ep.needsResId).toBe('boolean');
      expect(typeof ep.build).toBe('function');
    }
  });

  it('declares the correct id-needs per endpoint', () => {
    expect(byId('subscriptions')).toMatchObject({ needsSubId: false, needsResId: false });
    expect(byId('resource-groups')).toMatchObject({ needsSubId: true, needsResId: false });
    expect(byId('resources')).toMatchObject({ needsSubId: true, needsResId: false });
    expect(byId('resource-detail')).toMatchObject({ needsSubId: true, needsResId: true });
    expect(byId('role-assignments')).toMatchObject({ needsSubId: true, needsResId: false });
  });
});

describe('RUNNABLE_ENDPOINTS.build — path composition reuses queries.ts builders', () => {
  it('subscriptions builds "/subscriptions" with no id', () => {
    expect(byId('subscriptions').build({})).toBe('/subscriptions');
  });

  it('resource-groups builds exactly resourceGroupsUrl(sub)', () => {
    expect(byId('resource-groups').build({ sub: SAMPLE_SUB })).toBe(resourceGroupsUrl(SAMPLE_SUB));
  });

  it('resources builds exactly resourcesUrl(sub, rg)', () => {
    const path = byId('resources').build({ sub: SAMPLE_SUB, rg: SAMPLE_RG });
    expect(path).toBe(resourcesUrl(SAMPLE_SUB, SAMPLE_RG));
    expect(path.startsWith(`/subscriptions/${SAMPLE_SUB}/resourceGroups/`)).toBe(true);
  });

  it('resource-detail returns the full ARM id unchanged (armGet consumes it directly)', () => {
    expect(byId('resource-detail').build({ armId: SAMPLE_ARM_ID })).toBe(SAMPLE_ARM_ID);
  });

  it('role-assignments builds /subscriptions/{encoded sub}/providers/Microsoft.Authorization/roleAssignments', () => {
    expect(byId('role-assignments').build({ sub: SAMPLE_SUB })).toBe(
      `/subscriptions/${encodeURIComponent(SAMPLE_SUB)}/providers/Microsoft.Authorization/roleAssignments`,
    );
  });
});

describe('RUNNABLE_ENDPOINTS.build — WR-02 encoding + same-origin (T-18-02)', () => {
  it('encodeURIComponent-encodes an unsafe sub in every sub-composed path (no injected segment)', () => {
    // The per-segment WR-02 encoding applies to routes that COMPOSE a raw sub into the path.
    // resource-detail takes a pre-built full ARM id (needsResId) verbatim — WR-01/assertSameOrigin
    // covers it, and queries.ts intentionally does not re-encode a full armId — so it is excluded.
    const encoded = encodeURIComponent(UNSAFE_SUB);
    for (const ep of RUNNABLE_ENDPOINTS.filter((e) => e.needsSubId && !e.needsResId)) {
      const path = ep.build({ sub: UNSAFE_SUB, rg: SAMPLE_RG });
      // The encoded form appears...
      expect(path).toContain(encoded);
      // ...and the raw unsafe value does not leak (its `/` would inject an extra path segment).
      expect(path).not.toContain(UNSAFE_SUB);
    }
  });

  it('role-assignments keeps the sub in a single path segment even with a "/" in it', () => {
    const path = byId('role-assignments').build({ sub: UNSAFE_SUB });
    // '' + subscriptions + <enc-sub> + providers + Microsoft.Authorization + roleAssignments = 6 parts.
    expect(path.split('/')).toHaveLength(6);
  });

  it('every built path is same-origin root-relative (starts with "/", not "//", no scheme)', () => {
    const ids = { sub: SAMPLE_SUB, rg: SAMPLE_RG, armId: SAMPLE_ARM_ID };
    for (const ep of RUNNABLE_ENDPOINTS) {
      const path = ep.build(ids);
      expect(path.startsWith('/')).toBe(true);
      expect(path.startsWith('//')).toBe(false);
      expect(path).not.toMatch(/^[a-z]+:\/\//i);
    }
  });
});
