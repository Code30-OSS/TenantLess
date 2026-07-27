/**
 * DEMO-01 endpoint catalog — the "teach the contract" half of the scanner demo.
 *
 * A curated, offline-safe data module (NOT auto-enumerated, D-01b) of the nine headline ARM discovery
 * routes this simulator serves, grouped into five capability sections. Each entry carries a canned,
 * *static* `sample` that illustrates the response *shape* (D-01: orientation, not proof — never fetched
 * live). Keeping this as a pure data module makes DEMO-01 contract-testable (see `catalog.test.ts`) and
 * keeps the S1 view (18-04) declarative.
 *
 * The nine routes are VERIFIED against `mock-server/src/lib.rs` (build_router_without_sim, L82-168) and
 * `handlers/token.rs` (L38-41) — see 18-RESEARCH.md §"Enumerated ARM router". The server accepts any
 * api-version (MOCK-11), so the version strings are illustrative. Sample shapes are grounded in the
 * existing contracts in `api/types.ts` (ArmListEnvelope / ArmResourceGroup / ArmResourceSummary /
 * ArmResourceDetail) and the response shapes in `docs/vision/console-mockup/sim-api-spec.md`.
 *
 * Data boundary (D-05 / T-18-01): no forbidden OSS brand token and no sim-only / console / control /
 * drift / ui route appears anywhere here — both pinned by `catalog.test.ts`.
 */

/** One headline ARM route in the catalog. */
export interface CatalogEntry {
  /** HTTP verb the simulator serves for this route. */
  method: 'GET' | 'POST';
  /** Route template (path only, per-segment `{param}` placeholders). Always starts with `/`. */
  route: string;
  /** Illustrative Azure api-version (the server ignores it, MOCK-11). Absent for the token/JWKS routes. */
  apiVersion?: string;
  /** One-line description of what a scanner learns from this route. */
  purpose: string;
  /** A small, static response sample illustrating the shape (never null, never an array — MOCK-13 posture). */
  sample: unknown;
  /** The payload root the S1 JsonTree should display (`value` for list envelopes, `properties`/`keys`/… otherwise). */
  rootLabel: string;
}

/** A capability section grouping several headline routes. */
export interface CatalogGroup {
  /** Section heading (e.g. `DISCOVERY`). */
  title: string;
  entries: CatalogEntry[];
}

export const CATALOG: CatalogGroup[] = [
  {
    title: 'DISCOVERY',
    entries: [
      {
        method: 'GET',
        route: '/subscriptions',
        apiVersion: '2022-12-01',
        purpose: 'Enumerate every subscription in the tenant — the scan entry point.',
        rootLabel: 'value',
        sample: {
          value: [
            {
              id: '/subscriptions/6f8a2c1e-9b3d-4a5f-8c2e-1d7b4a9e0f33',
              subscriptionId: '6f8a2c1e-9b3d-4a5f-8c2e-1d7b4a9e0f33',
              displayName: 'Platform Production',
              state: 'Enabled',
              tenantId: 'c0a1b2c3-d4e5-46f7-8899-aabbccddeeff',
              subscriptionPolicies: {
                locationPlacementId: 'Public_2014-09-01',
                quotaId: 'EnterpriseAgreement_2014-09-01',
                spendingLimit: 'Off',
              },
            },
          ],
        },
      },
      {
        method: 'GET',
        route: '/subscriptions/{sub}/resourceGroups',
        apiVersion: '2021-04-01',
        purpose: 'List the resource groups within a subscription.',
        rootLabel: 'value',
        sample: {
          value: [
            {
              id: '/subscriptions/6f8a2c1e-9b3d-4a5f-8c2e-1d7b4a9e0f33/resourceGroups/rg-platform-prod',
              name: 'rg-platform-prod',
              location: 'eastus',
              type: 'Microsoft.Resources/resourceGroups',
              tags: { env: 'prod', costCenter: 'cc-1042' },
            },
          ],
        },
      },
      {
        method: 'GET',
        route: '/subscriptions/{sub}/resources',
        apiVersion: '2021-04-01',
        purpose: 'List every resource in a subscription; supports OData $filter (resourceType, tagName, tagValue, location).',
        rootLabel: 'value',
        sample: {
          value: [
            {
              id: '/subscriptions/6f8a2c1e-9b3d-4a5f-8c2e-1d7b4a9e0f33/resourceGroups/rg-platform-prod/providers/Microsoft.Storage/storageAccounts/stplatprod001',
              name: 'stplatprod001',
              type: 'Microsoft.Storage/storageAccounts',
              location: 'eastus',
              tags: { env: 'prod' },
            },
          ],
          nextLink:
            '/subscriptions/6f8a2c1e-9b3d-4a5f-8c2e-1d7b4a9e0f33/resources?api-version=2021-04-01&%24skiptoken=eyJwYWdlIjoyfQ',
        },
      },
    ],
  },
  {
    title: 'RESOURCE DETAIL',
    entries: [
      {
        method: 'GET',
        route: '/subscriptions/{sub}/resourceGroups/{rg}/providers/{provider}/{type}/{name}',
        apiVersion: '2021-04-01',
        purpose: 'Fetch a single resource with full properties; handles arbitrary nesting (e.g. servers/{n}/databases/{n}).',
        rootLabel: 'properties',
        sample: {
          id: '/subscriptions/6f8a2c1e-9b3d-4a5f-8c2e-1d7b4a9e0f33/resourceGroups/rg-platform-prod/providers/Microsoft.Sql/servers/sql-platform-prod',
          name: 'sql-platform-prod',
          type: 'Microsoft.Sql/servers',
          location: 'eastus',
          kind: 'v12.0',
          tags: { env: 'prod' },
          properties: {
            fullyQualifiedDomainName: 'sql-platform-prod.database.windows.net',
            version: '12.0',
            state: 'Ready',
            publicNetworkAccess: 'Disabled',
          },
          resources: [
            {
              id: '/subscriptions/6f8a2c1e-9b3d-4a5f-8c2e-1d7b4a9e0f33/resourceGroups/rg-platform-prod/providers/Microsoft.Sql/servers/sql-platform-prod/databases/orders',
              name: 'orders',
              type: 'Microsoft.Sql/servers/databases',
              location: 'eastus',
            },
          ],
        },
      },
    ],
  },
  {
    title: 'COST MANAGEMENT',
    entries: [
      {
        method: 'POST',
        route: '/subscriptions/{sub}/providers/Microsoft.CostManagement/query',
        apiVersion: '2025-03-01',
        purpose: 'Query synthetic cost, grouped by dimension or tag — a positional columns/rows result.',
        rootLabel: 'properties',
        sample: {
          properties: {
            columns: [
              { name: 'PreTaxCost', type: 'Number' },
              { name: 'ResourceType', type: 'String' },
              { name: 'Currency', type: 'String' },
            ],
            rows: [
              [1284.57, 'Microsoft.Compute/virtualMachines', 'USD'],
              [612.03, 'Microsoft.Storage/storageAccounts', 'USD'],
            ],
            nextLink: null,
          },
        },
      },
    ],
  },
  {
    title: 'AUTHORIZATION / RBAC',
    entries: [
      {
        method: 'GET',
        route: '/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions',
        apiVersion: '2022-04-01',
        purpose: 'List the role definitions available at the subscription scope (built-in role GUIDs).',
        rootLabel: 'value',
        sample: {
          value: [
            {
              id: '/subscriptions/6f8a2c1e-9b3d-4a5f-8c2e-1d7b4a9e0f33/providers/Microsoft.Authorization/roleDefinitions/b24988ac-6180-42a0-ab88-20f7382dd24c',
              name: 'b24988ac-6180-42a0-ab88-20f7382dd24c',
              type: 'Microsoft.Authorization/roleDefinitions',
              properties: {
                roleName: 'Contributor',
                type: 'BuiltInRole',
                description: 'Grants full access to manage all resources, but not assign roles.',
                permissions: [{ actions: ['*'], notActions: ['Microsoft.Authorization/*/Write'] }],
              },
            },
          ],
        },
      },
      {
        method: 'GET',
        route: '/subscriptions/{sub}/providers/Microsoft.Authorization/roleAssignments',
        apiVersion: '2022-04-01',
        purpose: 'List who (principal) holds which role at which scope — the assignment graph.',
        rootLabel: 'value',
        sample: {
          value: [
            {
              id: '/subscriptions/6f8a2c1e-9b3d-4a5f-8c2e-1d7b4a9e0f33/providers/Microsoft.Authorization/roleAssignments/3d2a1b0c-4e5f-4a6b-8c7d-9e0f1a2b3c4d',
              name: '3d2a1b0c-4e5f-4a6b-8c7d-9e0f1a2b3c4d',
              type: 'Microsoft.Authorization/roleAssignments',
              properties: {
                roleDefinitionId:
                  '/subscriptions/6f8a2c1e-9b3d-4a5f-8c2e-1d7b4a9e0f33/providers/Microsoft.Authorization/roleDefinitions/b24988ac-6180-42a0-ab88-20f7382dd24c',
                principalId: '7c9e6a5b-2d1f-4c3a-9b8e-0d4f6a1c2e3b',
                principalType: 'ServicePrincipal',
                scope: '/subscriptions/6f8a2c1e-9b3d-4a5f-8c2e-1d7b4a9e0f33',
              },
            },
          ],
        },
      },
    ],
  },
  {
    title: 'IDENTITY / TOKEN',
    entries: [
      {
        method: 'POST',
        route: '/{tenant}/oauth2/v2.0/token',
        purpose: 'Client-credentials token endpoint — returns a Bearer token (any token is accepted on the ARM routes).',
        rootLabel: 'token',
        sample: {
          token_type: 'Bearer',
          expires_in: 3599,
          ext_expires_in: 3599,
          access_token: 'eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJhdWQiOiJodHRwczovL21hbmFnZW1lbnQuYXp1cmUuY29tLyJ9.c2lnbmF0dXJl',
        },
      },
      {
        method: 'GET',
        route: '/{tenant}/discovery/v2.0/keys',
        purpose: 'JWKS endpoint — the public signing keys a scanner uses to validate the token.',
        rootLabel: 'keys',
        sample: {
          keys: [
            {
              kty: 'RSA',
              use: 'sig',
              kid: 'a1b2c3d4e5f60718293a4b5c6d7e8f90',
              n: '0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4',
              e: 'AQAB',
            },
          ],
        },
      },
    ],
  },
];
