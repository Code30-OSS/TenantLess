/**
 * TanStack Query hooks — the interface-first data contract every Explorer view consumes (D-04).
 *
 * These run under the no-poll QueryClient wired in `main.tsx` (staleTime Infinity, every implicit
 * refetch off — a static tenant, manual Refresh only). Each hook carries a stable, param-scoped
 * `queryKey` and an `enabled` guard so a missing param never fires a request. URLs are composed by
 * the pure builders below (also exported, unit-tested) so the fetch shape is provable without a DOM.
 *
 * Routing (RESEARCH Pattern 5): summary/violations/dependencies go through `simGet` (bearer-EXEMPT
 * `/_sim/**`); resource-groups/resources/detail go through `armGet` (placeholder Bearer on the ARM
 * `/subscriptions/**` gate). Mixing these up is the "KPIs render, tree 401s" failure — do not.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { armGet, simGet } from './client';
import { DEFAULT_TOP } from './odata';
import type {
  ArmListEnvelope,
  ArmResourceDetail,
  ArmResourceGroup,
  ArmResourceSummary,
  DependenciesResponse,
  ResourceSearchResponse,
  Severity,
  SubscriptionListResponse,
  Summary,
  ViolationsResponse,
} from './types';

// ---------------------------------------------------------------------------
// Pure URL builders (composed here, executed by the hooks; unit-tested directly)
// ---------------------------------------------------------------------------

export interface ViolationsParams {
  resource?: string;
  subscription?: string;
  severity?: Severity;
  code?: string;
}

/** `/_sim/violations` with resource/subscription scope + optional severity/code filters. */
export function violationsUrl(params: ViolationsParams): string {
  const q = new URLSearchParams();
  if (params.resource) q.set('resource', params.resource);
  if (params.subscription) q.set('subscription', params.subscription);
  if (params.severity) q.set('severity', params.severity);
  if (params.code) q.set('code', params.code);
  const qs = q.toString();
  return qs ? `/_sim/violations?${qs}` : '/_sim/violations';
}

export interface DependenciesParams {
  subscription?: string;
  type?: string;
  skipToken?: string;
  top?: number;
}

/** `/_sim/dependencies` with subscription/type filters + keyset `$skiptoken`. */
export function dependenciesUrl(params: DependenciesParams): string {
  const q = new URLSearchParams();
  if (params.subscription) q.set('subscription', params.subscription);
  if (params.type) q.set('type', params.type);
  if (params.top) q.set('$top', String(params.top));
  if (params.skipToken) q.set('$skiptoken', params.skipToken);
  const qs = q.toString();
  return qs ? `/_sim/dependencies?${qs}` : '/_sim/dependencies';
}

export interface SubscriptionsParams {
  top?: number;
  skipToken?: string;
}

/**
 * `/_sim/subscriptions` (bearer-EXEMPT, keyset-paginated on the UUID PK) with `$top` (defaults
 * {@link DEFAULT_TOP}) + optional `$skiptoken` (D-15). Mirrors {@link dependenciesUrl}: `URLSearchParams`
 * percent-encodes the `$` prefix to `%24`, which the axum `/_sim` parser percent-decodes.
 */
export function subscriptionsUrl(params: SubscriptionsParams = {}): string {
  const q = new URLSearchParams();
  q.set('$top', String(params.top ?? DEFAULT_TOP));
  if (params.skipToken) q.set('$skiptoken', params.skipToken);
  return `/_sim/subscriptions?${q.toString()}`;
}

export interface ResourceSearchParams {
  q: string;
  subscription?: string;
  skipToken?: string;
  top?: number;
}

/**
 * `/_sim/resources/search` (bearer-EXEMPT, tenant-wide name/type substring search, keyset-paginated
 * on the TEXT id PK) with `$top` (defaults {@link DEFAULT_TOP}), the search term `q`, and optional
 * `subscription` scope + `$skiptoken` (15-14). Mirrors {@link subscriptionsUrl}: `URLSearchParams`
 * percent-encodes the `$` prefix to `%24`, which the axum `SimQuery` percent-decodes.
 */
export function resourceSearchUrl(params: ResourceSearchParams): string {
  const q = new URLSearchParams();
  q.set('$top', String(params.top ?? DEFAULT_TOP));
  q.set('q', params.q);
  if (params.subscription) q.set('subscription', params.subscription);
  if (params.skipToken) q.set('$skiptoken', params.skipToken);
  return `/_sim/resources/search?${q.toString()}`;
}

export interface ResourceGroupsParams {
  skipToken?: string;
  top?: number;
}

/**
 * ARM resource-group list URL under a subscription with an optional `$top`/`$skiptoken` (nextLink
 * follow). With no params it returns the bare list path (byte-identical to the pre-pagination form).
 * The `sub` segment is `encodeURIComponent`-encoded so a deep-link value cannot inject path/query (WR-02).
 */
export function resourceGroupsUrl(sub: string, params: ResourceGroupsParams = {}): string {
  const base = `/subscriptions/${encodeURIComponent(sub)}/resourceGroups`;
  const q = new URLSearchParams();
  if (params.top) q.set('$top', String(params.top));
  if (params.skipToken) q.set('$skiptoken', params.skipToken);
  const qs = q.toString();
  return qs ? `${base}?${qs}` : base;
}

export interface ResourcesParams {
  filter?: string;
  skipToken?: string;
  top?: number;
}

/** ARM resource-list URL under an RG with `$top` (+ optional `$filter`/`$skiptoken`), server-side (D-04). */
export function resourcesUrl(sub: string, rg: string, params: ResourcesParams = {}): string {
  const q = new URLSearchParams();
  q.set('$top', String(params.top ?? DEFAULT_TOP));
  if (params.filter) q.set('$filter', params.filter);
  if (params.skipToken) q.set('$skiptoken', params.skipToken);
  // Encode each dynamic path segment so a `sub`/`rg` value (deep-link params) cannot inject
  // extra path/query into the ARM request (WR-02). armId is NOT encoded — it is a full ARM path
  // covered by the client's WR-01 same-origin guard.
  return `/subscriptions/${encodeURIComponent(sub)}/resourceGroups/${encodeURIComponent(rg)}/resources?${q.toString()}`;
}

// ---------------------------------------------------------------------------
// Query hooks
// ---------------------------------------------------------------------------

/** Tenant-summary aggregates for the header KPIs + tree counts (`/_sim/summary`, one-shot). */
export function useSummary(): UseQueryResult<Summary, Error> {
  return useQuery({
    queryKey: ['summary'],
    queryFn: () => simGet<Summary>('/_sim/summary'),
  });
}

/**
 * Full keyset-paginated subscription enumeration (`/_sim/subscriptions`, `simGet` — bearer-EXEMPT,
 * D-15). This is the tree's row source: it walks EVERY subscription across pages, replacing the
 * 500-capped `summary.subscriptions[]` preview (UAT Gap 2). Same-origin/no-auth via `simGet` — NOT
 * `armGet` (the `/_sim` overlay is bearer-exempt; using `armGet` here is the tree-401 regression).
 */
export function useSubscriptions(
  params: SubscriptionsParams = {},
): UseQueryResult<SubscriptionListResponse, Error> {
  return useQuery({
    queryKey: ['subscriptions', params.skipToken ?? null],
    queryFn: () => simGet<SubscriptionListResponse>(subscriptionsUrl(params)),
  });
}

/**
 * Resource groups under a subscription (ARM list; disabled until a subscription is chosen). Accepts
 * an optional `$top`/`$skiptoken` so a load-more can follow the ARM `nextLink` to the next page.
 */
export function useResourceGroups(
  sub: string | null | undefined,
  params: ResourceGroupsParams = {},
): UseQueryResult<ArmListEnvelope<ArmResourceGroup>, Error> {
  return useQuery({
    queryKey: ['resource-groups', sub, params.skipToken ?? null],
    queryFn: () => armGet<ArmListEnvelope<ArmResourceGroup>>(resourceGroupsUrl(sub!, params)),
    enabled: Boolean(sub),
  });
}

/** Resources under an RG (server-side `$filter`/pagination; disabled until sub+rg are chosen). */
export function useResources(
  sub: string | null | undefined,
  rg: string | null | undefined,
  params: ResourcesParams = {},
): UseQueryResult<ArmListEnvelope<ArmResourceSummary>, Error> {
  return useQuery({
    queryKey: ['resources', sub, rg, params.filter ?? null, params.skipToken ?? null],
    queryFn: () => armGet<ArmListEnvelope<ArmResourceSummary>>(resourcesUrl(sub!, rg!, params)),
    enabled: Boolean(sub) && Boolean(rg),
  });
}

/** Full ARM resource detail (the `properties` the JSON tree renders; disabled until selected). */
export function useResourceDetail(
  armId: string | null | undefined,
): UseQueryResult<ArmResourceDetail, Error> {
  return useQuery({
    queryKey: ['resource-detail', armId],
    queryFn: () => armGet<ArmResourceDetail>(armId!),
    enabled: Boolean(armId),
  });
}

/** Violations for a resource OR a subscription (`/_sim/violations`; disabled without a scope). */
export function useViolations(
  params: ViolationsParams,
): UseQueryResult<ViolationsResponse, Error> {
  const scoped = Boolean(params.resource) || Boolean(params.subscription);
  return useQuery({
    queryKey: [
      'violations',
      params.resource ?? null,
      params.subscription ?? null,
      params.severity ?? null,
      params.code ?? null,
    ],
    queryFn: () => simGet<ViolationsResponse>(violationsUrl(params)),
    enabled: scoped,
  });
}

/**
 * Tenant-wide resource search (`/_sim/resources/search`, `simGet` — bearer-EXEMPT, 15-14). The
 * `enabled` guard keeps it silent until a term is APPLIED: an empty/whitespace `q` fires no request
 * (the draft/applied search box only commits `q` on Enter/Apply, so no per-keystroke fetch). The
 * `queryKey` is param-scoped so a new term / page / scope is a distinct cache entry.
 */
export function useResourceSearch(
  params: ResourceSearchParams,
): UseQueryResult<ResourceSearchResponse, Error> {
  return useQuery({
    queryKey: [
      'resource-search',
      params.q,
      params.subscription ?? null,
      params.skipToken ?? null,
    ],
    queryFn: () => simGet<ResourceSearchResponse>(resourceSearchUrl(params)),
    enabled: params.q.trim() !== '',
  });
}

/** Cross-subscription dependencies edge list (`/_sim/dependencies`; EXPL-04). */
export function useDependencies(
  params: DependenciesParams = {},
): UseQueryResult<DependenciesResponse, Error> {
  return useQuery({
    queryKey: [
      'dependencies',
      params.subscription ?? null,
      params.type ?? null,
      params.skipToken ?? null,
    ],
    queryFn: () => simGet<DependenciesResponse>(dependenciesUrl(params)),
  });
}
