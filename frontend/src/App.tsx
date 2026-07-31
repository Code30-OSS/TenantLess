import { Routes, Route, Navigate } from 'react-router';

import AppShell from './shell/AppShell';
import ResourcesView from './explorer/ResourcesView';
import DependenciesView from './explorer/DependenciesView';
import ConsoleView from './console/ConsoleView';
import ControlPlaneView from './control-plane/ControlPlaneView';
import { JobProvider } from './control-plane/JobContext';
import OverviewView from './overview/OverviewView';
import CatalogView from './demo/CatalogView';
import ScannerConfigView from './demo/ScannerConfigView';
import ViewerView from './demo/ViewerView';

/**
 * The WEBUI-04 route table (relative to the router basename `/ui`, set in main.tsx).
 *
 * All five section routes are children of a single `<AppShell/>` layout route (the shell chrome
 * from 15-07): AppShell renders the sidebar + topbar and an `<Outlet/>` into which the matched
 * child view mounts. The index route redirects to the default Explorer view.
 *
 *   /ui/                     → redirect → /ui/overview                (D-04 front door re-point, 18-07)
 *   /ui/overview             → <OverviewView/>                        (PHASE 18 — S0 landing front door)
 *   /ui/explorer/resources   → <ResourcesView/>     (filled in 15-05)
 *   /ui/explorer/dependencies→ <DependenciesView/>  (filled in 15-06)
 *   /ui/console              → ConsoleView (element)                 (PHASE 16 — Observability Console)
 *   /ui/control-plane        → redirect → /control-plane/generate    (PHASE 17 — Control Plane)
 *   /ui/control-plane/:section → <ControlPlaneView/>                 (generate/analyze/tenants/server-status)
 *   /ui/demo                 → redirect → /demo/catalog              (PHASE 18 — Scanner Demo)
 *   /ui/demo/catalog         → <CatalogView/>                        (DEMO-01 endpoint catalog)
 *   /ui/demo/scanner         → <ScannerConfigView/>                  (DEMO-02 scanner config)
 *   /ui/demo/viewer          → <ViewerView/>                         (DEMO-03 live response viewer)
 *
 * ResourcesView / DependenciesView are minimal mount points here; 15-05 / 15-06 replace those
 * files wholesale — this route table (and App.tsx) does not change when the views land.
 */
export default function App() {
  return (
    // JobProvider sits ABOVE <Routes> (never unmounts on navigation), so a control-plane job that
    // reaches `succeeded` after the operator leaves the control plane still fires the completion-driven
    // full invalidation — stale-after-success closed even for the navigate-away-mid-job case.
    <JobProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="overview" replace />} />
          <Route path="overview" element={<OverviewView />} />
          <Route path="explorer/resources" element={<ResourcesView />} />
          <Route path="explorer/dependencies" element={<DependenciesView />} />
          <Route path="console" element={<ConsoleView />} />
          <Route path="control-plane" element={<Navigate to="/control-plane/generate" replace />} />
          <Route path="control-plane/:section" element={<ControlPlaneView />} />
          <Route path="demo" element={<Navigate to="/demo/catalog" replace />} />
          <Route path="demo/catalog" element={<CatalogView />} />
          <Route path="demo/scanner" element={<ScannerConfigView />} />
          <Route path="demo/viewer" element={<ViewerView />} />
        </Route>
      </Routes>
    </JobProvider>
  );
}
