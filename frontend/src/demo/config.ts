/**
 * DEMO-02 scanner-config DATA/BUILDER layer — the single tested source of the "point your scanner
 * here" strings rendered by the S2 view (18-05).
 *
 * Two load-bearing guarantees (pinned by config.test.ts, DOM-free):
 *   1. Every URL is interpolated from `window.location.origin` — NEVER a hardcoded host. Callers may
 *      inject an explicit `origin` (tests do) but production reads the live origin, mirroring the
 *      `assertSameOrigin` discipline in `api/client.ts`.
 *   2. The shipped env snippet uses generic, scanner-agnostic names with ZERO forbidden brand token.
 *      D-05 (locked 2026-07-10) is AUTHORITATIVE over the UI-SPEC's brand-named env copy: the OSS
 *      brand-token scrub gate forbids the vendor tokens with no allowlist, so the snippet ships as
 *      the generic SCANNER_ARM_ENDPOINT / SCANNER_STATIC_TOKEN names.
 */

/** Neutral, brand-free heading for the Path-A static-token env block (D-05). */
export const SCANNER_ENV_HEADING = 'STATIC-TOKEN SCANNER (PATH A)';

/** Body note under the env block — maps the generic names onto the operator's own scanner settings. */
export const ENV_BODY_NOTE =
  "Map these values to your scanner's endpoint and static-token settings.";

/**
 * Representative discovery api-version shown in the config page. Illustrative only: the mock accepts
 * any api-version (MOCK-11), so this is a realistic Azure value for orientation, not a constraint.
 */
export const API_VERSION = '2021-04-01';

/** Canned any-Bearer demo token — any non-empty token is accepted (D-03 / IAM-05). */
export const DEMO_BEARER = 'tenantless-demo';

/** The generic Authorization value shown alongside the explainer. */
export const AUTHORIZATION_VALUE = 'Bearer <any-non-empty-token>';

/** Short explainer for the any-Bearer contract (D-02a). */
export const AUTHORIZATION_EXPLAINER =
  'Any non-empty token is accepted — the mock serves with --enforce-auth OFF by default (IAM-05), ' +
  'so the Bearer is illustrative and never rejected on a read-only demo surface.';

/** Base URL = the live origin verbatim (D-02a) — never hardcoded. */
export function baseUrl(origin: string = window.location.origin): string {
  return origin;
}

/** Copyable curl one-liner against the discovery route, auto-authed with the canned demo Bearer. */
export function curlSnippet(origin: string = window.location.origin): string {
  // api-version matches the displayed API_VERSION so the copyable value and the curl example agree
  // (the mock accepts any version, MOCK-11 — this is demo consistency, not a constraint).
  return `curl -H "Authorization: Bearer ${DEMO_BEARER}" "${origin}/subscriptions?api-version=${API_VERSION}"`;
}

/**
 * Copyable generic scanner env block (D-05). Two lines under the neutral heading; the origin is the
 * only interpolated value, the token is the canned any-Bearer demo literal.
 */
export function envSnippet(origin: string = window.location.origin): string {
  return [
    `# ${SCANNER_ENV_HEADING}`,
    `SCANNER_ARM_ENDPOINT=${origin}`,
    `SCANNER_STATIC_TOKEN=${DEMO_BEARER}`,
  ].join('\n');
}
