/**
 * DEMO-03 runnable-endpoint MODEL — the pure, DOM-free table the live viewer (18-06) consumes.
 *
 * The S3 viewer only runs GET ARM endpoints that `armGet` supports AND that resolve against the
 * current tenant. This module encodes "which endpoints are runnable + how each one's concrete
 * same-origin ARM path is composed from real ids" as data, so the view stays declarative and path
 * composition is provable without a DOM.
 *
 * Path composition REUSES the exported, unit-tested `queries.ts` builders
 * (`resourceGroupsUrl`/`resourcesUrl`, per-segment `encodeURIComponent`, WR-02) — never string
 * concat for the list routes, never a new fetch. For the resource-detail route, `armGet` takes the
 * full ARM id verbatim, so `build` returns it unchanged. Cost (POST) and the AAD token/JWKS routes
 * are catalog-only, NOT runnable (D-03) and are deliberately absent here.
 *
 * Every produced path is root-relative (`/…`), so `armGet`'s `assertSameOrigin` (WR-01) fails-closed
 * on anything this model can never emit — an absolute or cross-origin URL (T-18-02).
 */
import { resourceGroupsUrl, resourcesUrl } from '../api/queries';

/** The concrete ids a `build` may consume — each endpoint reads only the ones it declares. */
export interface EndpointIds {
  sub?: string;
  rg?: string;
  armId?: string;
}

/** A single runnable ARM route the live viewer can execute (GET-only, D-03). */
export interface RunnableEndpoint {
  /** Stable machine id (matches the DEMO-03 test + the S3 select). */
  id: string;
  /** Human label for the viewer's endpoint picker. */
  label: string;
  /** Always 'GET' — the runnable set has no POST/mutation (T-18-07). */
  method: 'GET';
  /** Whether the route needs a real subscription id to resolve. */
  needsSubId: boolean;
  /** Whether the route needs a real resource id (full ARM id) to resolve. */
  needsResId: boolean;
  /** Compose the concrete same-origin ARM path from the supplied real ids. */
  build(ids: EndpointIds): string;
}

/**
 * The 5 runnable GET endpoints (D-03). Ordered discovery → detail → authorization, matching the S3
 * viewer's natural drill-down.
 */
export const RUNNABLE_ENDPOINTS: RunnableEndpoint[] = [
  {
    id: 'subscriptions',
    label: 'List subscriptions',
    method: 'GET',
    needsSubId: false,
    needsResId: false,
    build: () => '/subscriptions',
  },
  {
    id: 'resource-groups',
    label: 'List resource groups',
    method: 'GET',
    needsSubId: true,
    needsResId: false,
    build: ({ sub }) => resourceGroupsUrl(sub ?? ''),
  },
  {
    id: 'resources',
    label: 'List resources in a group',
    method: 'GET',
    needsSubId: true,
    needsResId: false,
    build: ({ sub, rg }) => resourcesUrl(sub ?? '', rg ?? ''),
  },
  {
    id: 'resource-detail',
    label: 'Get resource detail',
    method: 'GET',
    needsSubId: true,
    needsResId: true,
    // armGet consumes the full ARM id directly; return it verbatim (WR-01 same-origin guard applies).
    build: ({ armId }) => armId ?? '',
  },
  {
    id: 'role-assignments',
    label: 'List role assignments',
    method: 'GET',
    needsSubId: true,
    needsResId: false,
    build: ({ sub }) =>
      `/subscriptions/${encodeURIComponent(sub ?? '')}/providers/Microsoft.Authorization/roleAssignments`,
  },
];
