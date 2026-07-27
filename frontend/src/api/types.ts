/**
 * Response-shape types for the `/_sim` overlay and the ARM data routes the Explorer consumes.
 *
 * Casing is **camelCase** verbatim from `docs/vision/console-mockup/sim-api-spec.md` (the Phase-14
 * `/_sim` contract) and the ARM list/detail envelopes (MOCK-03/05/08/13). These are the single
 * contract 15-05 (tree) and 15-06 (dependency table) build against — do not drift them.
 */

/** Governance-violation severity (drives the chip color: High→red, Medium→amber, Low→green). */
export type Severity = 'High' | 'Medium' | 'Low';

/** ARM `{ error: { code, message } }` body (CloudError shape, MOCK-10). */
export interface ArmErrorBody {
  error: { code: string; message: string };
}

/** `GET /_sim/summary` — one-shot tenant aggregates for the Explorer header + tree counts (WAPI-03). */
export interface Summary {
  tenantId: string;
  seed: number;
  profile: string;
  totals: SummaryTotals;
  subscriptions: SummarySubscription[];
  byType: TypeCount[];
  byLocation: LocationCount[];
}

export interface SummaryTotals {
  subscriptions: number;
  resourceGroups: number;
  resources: number;
  violations: number;
  dependencies: number;
}

/** Per-subscription rollup row (note: `violationCount` is the per-sub badge; there is no `subscriptionCount`). */
export interface SummarySubscription {
  subscriptionId: string;
  name: string;
  archetype: string;
  resourceCount: number;
  resourceGroupCount: number;
  violationCount: number;
}

/**
 * `GET /_sim/subscriptions` — the keyset-paginated FULL subscription enumeration (WAPI-03 / D-15).
 * Same per-subscription rollup rows as the inline `summary.subscriptions[]` preview, but unbounded:
 * `count` is the whole-tenant total, `nextLink` carries the `$skiptoken` continuation until the last
 * page. This is the tree's row source — it supersedes the 500-capped summary preview (UAT Gap 2).
 */
export interface SubscriptionListResponse {
  count: number;
  value: SummarySubscription[];
  nextLink?: string;
}

/**
 * One `GET /_sim/resources/search` hit (15-14, EXPL-01/EXPL-05) — the exact camelCase DTO the
 * Rust `ResourceSearchDto` emits: the SAME id/name/type/subscription/RG the tree already exposes.
 */
export interface ResourceSearchResult {
  id: string;
  name: string;
  type: string;
  subscriptionId: string;
  resourceGroupName: string;
}

/**
 * One matching-subscription hit on a `GET /_sim/resources/search` response (EXPL-GAP-01, 15-15). The
 * backend `search_where` also matches the term against subscription display names and returns the
 * bounded (≤ SEARCH_SUBSCRIPTIONS_CAP), name-ASC set of matching subscriptions here — `id` is the
 * subscription UUID, `name` its synthetic display name. Empty `[]` when no subscription name matches.
 */
export interface SearchSubscriptionHit {
  id: string;
  name: string;
}

/**
 * One matching-resource-group hit on a `GET /_sim/resources/search` response (RG-name search). A
 * resource group has no standalone UUID — it is addressed by `subscriptionId` + `name`, which
 * together are the Miller-column selection key (`?sub&rg`). Resources are named unlike their RGs,
 * so an RG-name term (e.g. `rg-corp-...`) matches zero resource rows and surfaces only here.
 * Bounded (≤ SEARCH_RESOURCE_GROUPS_CAP), name-ASC; empty `[]` when no RG name matches.
 */
export interface SearchResourceGroupHit {
  name: string;
  subscriptionId: string;
}

/** `GET /_sim/resources/search` envelope — keyset-paginated: `count` is the full filtered total. */
export interface ResourceSearchResponse {
  count: number;
  value: ResourceSearchResult[];
  /** Subscriptions whose NAME matched the term (EXPL-GAP-01); bounded, name-ASC, `[]` when none match. */
  subscriptions: SearchSubscriptionHit[];
  /** Resource groups whose NAME matched the term (RG-name search); bounded, name-ASC, `[]` when none. */
  resourceGroups: SearchResourceGroupHit[];
  nextLink?: string;
}

export interface TypeCount {
  type: string;
  count: number;
}

export interface LocationCount {
  location: string;
  count: number;
}

/** A single governance violation (`GET /_sim/violations`, WAPI-01). */
export interface Violation {
  resourceId: string;
  code: string;
  severity: Severity;
  subscriptionId: string;
  detail: unknown;
}

/** `GET /_sim/violations` envelope. */
export interface ViolationsResponse {
  count: number;
  value: Violation[];
}

/** One endpoint of a dependency edge. */
export interface DependencyEndpoint {
  resourceId: string;
  subscriptionId: string;
}

/** A cross-resource dependency edge (`GET /_sim/dependencies`, WAPI-02). */
export interface Dependency {
  type: string;
  source: DependencyEndpoint;
  target: DependencyEndpoint;
  crossSubscription: boolean;
}

/** `GET /_sim/dependencies` envelope (keyset-paginated). */
export interface DependenciesResponse {
  count: number;
  value: Dependency[];
  nextLink?: string;
}

/** Generic ARM list envelope `{ value, nextLink }` (MOCK-03/08). */
export interface ArmListEnvelope<T> {
  value: T[];
  nextLink?: string;
}

/** ARM resource-group summary row (list route). */
export interface ArmResourceGroup {
  id: string;
  name: string;
  location: string;
  type?: string;
  tags?: Record<string, string>;
}

/** ARM resource summary row (list route). */
export interface ArmResourceSummary {
  id: string;
  name: string;
  type: string;
  location: string;
  tags?: Record<string, string>;
}

/**
 * ARM resource-detail shape (arbitrary nesting, MOCK-05). `properties` is ALWAYS an object,
 * never null (MOCK-13) — the JSON tree relies on that. `resources` is the optional nested-children
 * array some ARM detail responses carry (rendered as the "◆ nested resources" block when present).
 */
export interface ArmResourceDetail {
  id: string;
  name: string;
  type: string;
  location: string;
  tags?: Record<string, string>;
  sku?: unknown;
  kind?: string;
  properties: Record<string, unknown>;
  resources?: ArmResourceSummary[];
}

// ---------------------------------------------------------------------------
// Control plane (Phase 17, CTRL-01/CTRL-02) — the write-surface DTOs the
// GenerateForm/AnalyzeForm/JobPanel/TenantsManager consume. Casing matches the
// Rust `/_control` bodies verbatim (job.rs / control.rs, 17-01/17-02): the job
// wire `status` is lowercase and the parsed generate summary is snake_case.
// ---------------------------------------------------------------------------

/**
 * `POST /_control/generate` body. Fields map 1:1 to the `generate` CLI flags (D-08), clamped by the
 * server-side D-03 caps. `violations` is the on/off the UI slider maps to (`--violations`/
 * `--no-violations`, NOT a granular rate); `over_privilege` toggles the over-privilege injection.
 */
export interface GenerateArgs {
  profile: string;
  seed: number;
  resources: number;
  subscriptions: number;
  jobs: number;
  violations: boolean;
  over_privilege: boolean;
}

/**
 * `POST /_control/analyze` body (D-12). `source` is a server-owned allowlisted DuckDB source stem
 * (never a path/upload); `out_name` is the safe-name derived-profile stem that then appears in the
 * generate PROFILE allowlist.
 */
export interface AnalyzeArgs {
  source: string;
  out_name: string;
}

/** Job lifecycle wire status (lowercase serde from `JobStatus`, job.rs). No cancel this phase (D-15). */
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed';

/**
 * Parsed generate/analyze summary (job.rs `parse_generate_summary`, snake_case). All fields optional —
 * a job can succeed on exit-0 with an unparsed summary (D-08), in which case `result` is absent.
 */
export interface JobResult {
  tenant_id?: string;
  subscriptions?: number;
  resource_groups?: number;
  resources?: number;
  violations?: number;
}

/** `GET /_control/jobs/{id}` snapshot: lowercase `status` + coarse `phase` label + bounded `log` tail. */
export interface JobSnapshot {
  status: JobStatus;
  phase?: string;
  log: string[];
  result?: JobResult;
}

/**
 * A named server-side tenant snapshot (`GET /_control/snapshots`, CTRL-04 / 17-04). Mirrors the
 * server's authoritative wire shape (snapshot.rs `SnapshotEntry`): the bare safe-name stem + the
 * artifact mtime as Unix SECONDS (`created_unix` renamed `createdUnix`). The server does not
 * report a size.
 */
export interface Snapshot {
  name: string;
  createdUnix: number;
}

/** One allowlisted analyze source stem (`GET /_control/sources`, 17-02). */
export interface ControlSource {
  name: string;
}

/** One generate-profile stem — bundled or derived (`GET /_control/profiles`, 17-02). */
export interface ControlProfile {
  name: string;
}
