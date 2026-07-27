/**
 * Pure OData helpers for the Explorer (EXPL-05 groundwork). No fetch, no React — these only
 * COMPOSE the query string and PARSE a continuation cursor out of a nextLink. The axum server
 * remains the fail-closed authority: it validates + parameter-binds every `$filter` and returns an
 * ARM `{ error: { code, message } }` 400 on a bad one (surfaced inline by the view layer in 15-05).
 *
 * Design notes:
 * - buildFilter never trusts client-side validation as authoritative; it just assembles the string
 *   the guided pickers / raw input produce, escaping quotes so a value can't break out of the
 *   literal (the server still binds it as $N — this escaping is for a well-formed OData string).
 * - parseSkipToken / parseTop read the continuation out of an absolute ARM `nextLink` so the UI can
 *   re-request its OWN relative path instead of blindly following the server-origin URL (RESEARCH
 *   Pitfall 3 — the nextLink origin comes from the server's base_url and won't match the embedded
 *   prod origin). They accept BOTH `$skipToken` (spec casing) and `$skiptoken` (the lowercase form
 *   the axum server actually serializes) and never throw on null/undefined.
 */

/** OData filter fields the mock server's `$filter` surface accepts (MOCK-06). */
export type FilterField = 'resourceType' | 'location' | 'tagName' | 'tagValue';

/** A single `<field> eq '<value>'` filter clause. */
export interface FilterClause {
  field: FilterField;
  value: string;
}

/** ARM default page size when `$top` is absent (mirrors the server's `DEFAULT_TOP`, pagination.rs). */
export const DEFAULT_TOP = 100;

/**
 * Compose an OData `$filter` string from clauses, joined with ` and ` in input order.
 * Returns `""` for an empty list (the caller then omits the `$filter` param entirely).
 * Single quotes in a value are escaped by OData `''` doubling.
 */
export function buildFilter(clauses: readonly FilterClause[]): string {
  return clauses
    .map((clause) => `${clause.field} eq '${escapeODataLiteral(clause.value)}'`)
    .join(' and ');
}

/** Escape a string literal for embedding in an OData `'...'` value ('' doubling). */
function escapeODataLiteral(value: string): string {
  return value.replace(/'/g, "''");
}

/**
 * Parse the opaque keyset cursor out of a nextLink. Accepts `$skipToken` and `$skiptoken`.
 * Returns `null` when absent, or for a null/undefined/empty input (never throws).
 */
export function parseSkipToken(nextLink: string | null | undefined): string | null {
  const params = queryParamsOf(nextLink);
  if (!params) return null;
  return params.get('$skipToken') ?? params.get('$skiptoken') ?? null;
}

/**
 * Parse `$top` out of a nextLink as a positive integer. Falls back to `fallback` (default
 * {@link DEFAULT_TOP}) when `$top` is absent, non-numeric, non-integer, or non-positive.
 * Never throws on null/undefined.
 */
export function parseTop(
  nextLink: string | null | undefined,
  fallback: number = DEFAULT_TOP,
): number {
  const params = queryParamsOf(nextLink);
  if (!params) return fallback;
  const raw = params.get('$top');
  if (raw === null) return fallback;
  const n = Number(raw);
  return Number.isInteger(n) && n > 0 ? n : fallback;
}

/**
 * Extract just the query params from a link string, tolerating both absolute and relative URLs
 * (we only ever read the query — we never resolve/follow the origin). Returns `null` for a
 * null/undefined/empty link.
 */
function queryParamsOf(link: string | null | undefined): URLSearchParams | null {
  if (!link) return null;
  const q = link.indexOf('?');
  if (q === -1) return new URLSearchParams();
  return new URLSearchParams(link.slice(q + 1));
}
