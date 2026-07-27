/**
 * The sidebar navigation model (WEBUI-04).
 *
 * Data-driven: `SidebarNav` iterates this array of groups → items, so adding a section later
 * (e.g. the Identity/RBAC item that exists in the mockup but is out of scope this phase, per
 * 15-RESEARCH) is an APPEND-ONLY edit here — no consumer component changes. To add Identity/RBAC
 * later, push `{ label: 'Identity', to: '/explorer/identity' }` onto the Explorer group's `items`
 * (or add a new group); `SidebarNav` renders it with zero edits.
 *
 * Only Explorer is built this phase; Console / Control Plane / Demo route to the shared ComingSoon
 * stub (Phases 16 / 17 / 18).
 */

export interface NavItem {
  /** Visible link label (Space Mono). */
  label: string;
  /** Absolute in-app path (resolved under the router basename `/ui`). */
  to: string;
}

export interface NavGroup {
  /** Uppercase eyebrow heading for the group. */
  title: string;
  items: NavItem[];
}

/** The five top-level sections. Overview is the front door; Explorer and Demo have multiple items. */
export const NAV_MODEL: NavGroup[] = [
  {
    // The S0 landing front door, prepended as the first group (append-only, D-04 / 18-07).
    title: 'Overview',
    items: [{ label: 'Overview', to: '/overview' }],
  },
  {
    title: 'Explorer',
    items: [
      { label: 'Resources', to: '/explorer/resources' },
      { label: 'Dependencies', to: '/explorer/dependencies' },
      // Identity/RBAC intentionally omitted this phase — append here later (no consumer edits).
    ],
  },
  {
    title: 'Console',
    items: [{ label: 'Console', to: '/console' }],
  },
  {
    // Expanded from the single Phase-17 stub to the four control-plane sections (append-only, D-16).
    title: 'Control Plane',
    items: [
      { label: 'generate', to: '/control-plane/generate' },
      { label: 'analyze', to: '/control-plane/analyze' },
      { label: 'tenants', to: '/control-plane/tenants' },
      { label: 'server status', to: '/control-plane/server-status' },
    ],
  },
  {
    // Expanded from the single Phase-18 stub to the three demo sub-pages (append-only, 18-07).
    title: 'Demo',
    items: [
      { label: 'catalog', to: '/demo/catalog' },
      { label: 'scanner', to: '/demo/scanner' },
      { label: 'viewer', to: '/demo/viewer' },
    ],
  },
];
