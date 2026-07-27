import { describe, it, expect } from 'vitest';

import { resourceSearchUrl, subscriptionsUrl } from './queries';

/**
 * Pure-builder contract for the full-tenant subscription enumeration source (UAT Gap 2, D-15).
 *
 * `subscriptionsUrl` composes the bearer-EXEMPT `/_sim/subscriptions` URL that `useSubscriptions`
 * fetches via `simGet` — the keyset-paginated FULL enumeration that replaces the 500-capped
 * `summary.subscriptions[]` preview as the tree's row source. Encoding mirrors the sibling builders
 * (`dependenciesUrl`/`resourcesUrl`): `URLSearchParams` percent-encodes the `$` prefix to `%24`, and
 * the axum server percent-decodes it (proven by the existing `/_sim/dependencies` consumer).
 */
describe('subscriptionsUrl — /_sim/subscriptions builder (WAPI-03 / D-15)', () => {
  it('defaults $top to DEFAULT_TOP (100) with no params', () => {
    expect(subscriptionsUrl({})).toBe('/_sim/subscriptions?%24top=100');
  });

  it('sets $top and $skiptoken when given', () => {
    const url = subscriptionsUrl({ top: 50, skipToken: 'abc' });
    expect(url).toContain('%24top=50');
    expect(url).toContain('%24skiptoken=abc');
  });

  it('targets the bearer-EXEMPT /_sim path (same-origin, simGet)', () => {
    expect(subscriptionsUrl({}).startsWith('/_sim/')).toBe(true);
  });
});

/**
 * Pure-builder contract for the tenant-wide resource search source (15-14, EXPL-01/EXPL-05).
 *
 * `resourceSearchUrl` composes the bearer-EXEMPT `/_sim/resources/search` URL that
 * `useResourceSearch` fetches via `simGet`. Encoding mirrors the sibling builders: `$top` defaults to
 * DEFAULT_TOP and percent-encodes to `%24top`; `q` is carried; `subscription` + `$skiptoken` (as
 * `%24skiptoken`) appear only when provided. The `$`-prefixed keys percent-encode to `%24`, which the
 * axum `SimQuery` percent-decodes.
 */
describe('resourceSearchUrl — /_sim/resources/search builder (EXPL-01/EXPL-05)', () => {
  it('defaults $top to DEFAULT_TOP (100) and carries q, no subscription/$skiptoken by default', () => {
    const url = resourceSearchUrl({ q: 'stor' });
    expect(url).toContain('%24top=100');
    expect(url).toContain('q=stor');
    expect(url).not.toContain('subscription=');
    expect(url).not.toContain('%24skiptoken=');
  });

  it('adds subscription and $skiptoken (as %24skiptoken) only when provided', () => {
    const url = resourceSearchUrl({ q: 'x', subscription: 'sub-a', skipToken: 'TOK', top: 25 });
    expect(url).toContain('%24top=25');
    expect(url).toContain('q=x');
    expect(url).toContain('subscription=sub-a');
    expect(url).toContain('%24skiptoken=TOK');
  });

  it('targets the bearer-EXEMPT /_sim/resources/search path (same-origin, simGet)', () => {
    expect(resourceSearchUrl({ q: 'x' }).startsWith('/_sim/resources/search')).toBe(true);
  });
});
