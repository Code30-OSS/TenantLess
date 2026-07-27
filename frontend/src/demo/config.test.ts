import { describe, it, expect } from 'vitest';

import { forbiddenTokenPattern } from '../test-utils/scrubTokens';

import {
  API_VERSION,
  AUTHORIZATION_EXPLAINER,
  DEMO_BEARER,
  SCANNER_ENV_HEADING,
  baseUrl,
  curlSnippet,
  envSnippet,
} from './config';

/**
 * DEMO-02 scanner-config DATA/BUILDER contract (pure, DOM-free — mirrors api/queries.test.ts).
 *
 * These builders produce the copyable "point your scanner here" strings for the S2 view (18-05).
 * The two load-bearing guarantees proven here — WITHOUT a DOM — are:
 *   1. every URL is interpolated from an INJECTED origin (never a hardcoded host), and
 *   2. the shipped snippet carries generic, scanner-agnostic env names with ZERO forbidden brand
 *      token (D-05, locked AUTHORITATIVE over the UI-SPEC's literal vendor-branded env copy).
 *
 * Origin is injected as an explicit argument so the assertions never touch global window state.
 */
const ORIGIN = 'https://tenant.demo.example:4321';
// Forbidden tokens come from tests/scrub-tokens.json plus the gitignored private
// supplement -- never spelled in this source. They used to be assembled from string
// fragments, which defeated the public/private split: deleting the `+` signs
// reconstructed the private word list from a public file.
const FORBIDDEN_BRAND = forbiddenTokenPattern();
const HARDCODED_ORIGIN = /localhost|127\.0\.0\.1|http:\/\/|:8080|:8443/;

/** Every string this module produces for the injected origin. */
function allProduced(origin: string): string[] {
  return [baseUrl(origin), curlSnippet(origin), envSnippet(origin), API_VERSION, AUTHORIZATION_EXPLAINER];
}

describe('config.baseUrl — origin-derived base URL (D-02a)', () => {
  it('equals the injected origin verbatim', () => {
    expect(baseUrl(ORIGIN)).toBe(ORIGIN);
  });
});

describe('config.curlSnippet — copyable curl one-liner (D-02b)', () => {
  it('interpolates the injected origin', () => {
    expect(curlSnippet(ORIGIN)).toContain(ORIGIN);
  });

  it('carries the canned any-Bearer demo token and the discovery path', () => {
    const curl = curlSnippet(ORIGIN);
    expect(curl).toContain(`Authorization: Bearer ${DEMO_BEARER}`);
    expect(DEMO_BEARER).toBe('tenantless-demo');
    expect(curl).toContain('/subscriptions?api-version=');
  });
});

describe('config.envSnippet — generic scanner env block (D-05)', () => {
  it('uses the generic SCANNER_ env names with the injected origin', () => {
    const env = envSnippet(ORIGIN);
    expect(env).toContain(`SCANNER_ARM_ENDPOINT=${ORIGIN}`);
    expect(env).toContain('SCANNER_STATIC_TOKEN=tenantless-demo');
  });

  it('carries the neutral static-token heading', () => {
    expect(envSnippet(ORIGIN)).toContain('STATIC-TOKEN SCANNER (PATH A)');
    expect(SCANNER_ENV_HEADING).toBe('STATIC-TOKEN SCANNER (PATH A)');
  });
});

describe('config — no hardcoded origin (T-18-06)', () => {
  it('no produced string contains a hardcoded literal host/port', () => {
    for (const s of allProduced(ORIGIN)) {
      expect(s).not.toMatch(HARDCODED_ORIGIN);
    }
  });
});

describe('config — brand-free (D-05 / T-18-01)', () => {
  it('no produced string matches a forbidden OSS brand token', () => {
    // A null pattern means no tokens were configured; passing it to toMatch
    // would assert nothing at all.
    expect(FORBIDDEN_BRAND).not.toBeNull();
    expect(JSON.stringify(allProduced(ORIGIN))).not.toMatch(FORBIDDEN_BRAND!);
  });
});

describe('config — representative api-version + any-Bearer explainer (D-02a, MOCK-11, IAM-05)', () => {
  it('exposes the representative discovery api-version 2021-04-01', () => {
    expect(API_VERSION).toBe('2021-04-01');
  });

  it('explains the any-non-empty-token contract with IAM-05 / --enforce-auth semantics', () => {
    expect(AUTHORIZATION_EXPLAINER).toMatch(/any non-empty/i);
    expect(AUTHORIZATION_EXPLAINER).toContain('IAM-05');
    expect(AUTHORIZATION_EXPLAINER).toContain('--enforce-auth');
  });
});
