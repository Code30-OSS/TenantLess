/**
 * ControlPlaneView (Phase 17, 17-UI-SPEC §Auth Surfacing) — the control-plane route container.
 *
 * On mount it probes `GET /_control/probe` ONCE and renders one of three top-level auth states that
 * gate the ENTIRE section:
 *   - Disarmed (probe 404): a centered explainer card (ComingSoon layout idiom), no forms, no token.
 *   - Armed, no token (probe 401 / not yet unlocked): a `ControlTokenGate` above blurred/disabled forms.
 *   - Armed + valid (probe 2xx after unlock): full forms + a slim `control token active` lock affordance.
 *
 * It hosts a URL-driven section switch (generate / analyze / tenants / server-status — mounted at the
 * `/control-plane/:section` route, 17-06) and a persistent single-writer `BusyLockBanner` (D-11) that
 * is visible across all screens while the active job is `queued`/`running` — derived from `useJob`.
 *
 * The active job is owned by the app-level `JobProvider` (JobContext, mounted above <Routes> so it
 * survives route changes); this view is a pure CONSUMER via `useJobContext()`. Forms report a started
 * job via `onStarted = reportJob`, and the busy state (which disables every start-action) is read from
 * the provider — so a job that succeeds after the operator leaves this view still invalidates the cache.
 */
import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';

import { ArmError } from '../api/client';
import { controlGet, setControlToken } from '../api/control';
import AnalyzeForm from './AnalyzeForm';
import ControlTokenGate from './ControlTokenGate';
import GenerateForm from './GenerateForm';
import { useJobContext } from './JobContext';
import ServerStatusView from './ServerStatusView';
import TenantsManager from './TenantsManager';
import styles from './controls.module.css';

/** The shared prop contract every control-plane section form implements. */
export interface ControlSectionProps {
  /** True while ANY job is in flight — the form disables all start-actions (single-writer, D-11). */
  busy: boolean;
  /** The active job id (rendered by the form's own JobPanel), or null. */
  activeJobId: string | null;
  /** Report a freshly started job so the container can track busy + render the active-job strip. */
  onStarted: (jobId: string) => void;
}

type ArmState = 'loading' | 'disarmed' | 'armed';
type Section = 'generate' | 'analyze' | 'tenants' | 'server-status';

const SECTIONS: { key: Section; label: string }[] = [
  { key: 'generate', label: 'generate' },
  { key: 'analyze', label: 'analyze' },
  { key: 'tenants', label: 'tenants' },
  { key: 'server-status', label: 'server status' },
];

export default function ControlPlaneView() {
  const navigate = useNavigate();
  const params = useParams();
  // The active section is URL-driven (route `/control-plane/:section`): the App.tsx index route
  // redirects `/control-plane` → `/control-plane/generate`, and the section nav navigates between
  // the four sibling paths. An unknown/missing param falls back to `generate`.
  const section: Section = SECTIONS.some((s) => s.key === params.section)
    ? (params.section as Section)
    : 'generate';

  const [arm, setArm] = useState<ArmState>('loading');
  const [unlocked, setUnlocked] = useState(false);

  // The active job (id, busy state, and the completion-driven full invalidation) is owned by the
  // app-level JobProvider so it survives navigation away from this view. This view only
  // consumes it: forms report a started job through `reportJob`.
  const { activeJobId, busy, reportJob } = useJobContext();

  // Probe ONCE on mount to distinguish disarmed (404) from armed (needs a token).
  useEffect(() => {
    let cancelled = false;
    controlGet('/_control/probe')
      .then(() => {
        if (cancelled) return;
        // A 2xx means the plane is armed AND the in-memory token (if any) is already valid.
        setArm('armed');
        setUnlocked(true);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ArmError && err.status === 404) setArm('disarmed');
        else setArm('armed');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function lock() {
    setControlToken(null);
    setUnlocked(false);
  }

  if (arm === 'loading') {
    return (
      <div className={styles.view}>
        <p className={styles.statusLine}>connecting…</p>
      </div>
    );
  }

  if (arm === 'disarmed') {
    return (
      <div className={styles.disarmed}>
        <div className={styles.mark} aria-hidden="true">
          ◆
        </div>
        <h1 className={styles.disarmedTitle}>Control plane not enabled</h1>
        <p className={styles.disarmedBody}>
          This server is read-only. Restart it with --enable-control-plane and a control token to
          enable generate, analyze, snapshots and reset.
        </p>
        <code className={styles.cmdHint}>tenantless serve --enable-control-plane</code>
      </div>
    );
  }

  const sectionProps: ControlSectionProps = { busy, activeJobId, onStarted: reportJob };

  return (
    <div className={styles.view}>
      <div>
        <div className={styles.eyebrow}>◆ CONTROL PLANE · {section.replace('-', ' ').toUpperCase()}</div>
      </div>

      <nav className={styles.sectionNav} aria-label="Control plane sections">
        {SECTIONS.map((s) => (
          <button
            key={s.key}
            type="button"
            className={s.key === section ? `${styles.navBtn} ${styles.navBtnActive}` : styles.navBtn}
            aria-current={s.key === section ? 'page' : undefined}
            onClick={() => navigate(`/control-plane/${s.key}`)}
          >
            {s.label}
          </button>
        ))}
      </nav>

      {busy && (
        <div className={styles.busyBanner} role="status">
          <span className={styles.busyText}>A job is running — control actions are paused.</span>
          <span className={styles.busyCaption}>
            In-flight ARM reads may briefly pause while the tenant is written.
          </span>
        </div>
      )}

      {!unlocked ? (
        <>
          <ControlTokenGate onUnlocked={() => setUnlocked(true)} />
          <div className={styles.locked} aria-hidden="true">
            <SectionBody section={section} {...sectionProps} busy />
          </div>
        </>
      ) : (
        <>
          <div className={styles.lockStrip}>
            <span>control token active</span>
            <button type="button" className={styles.lockBtn} onClick={lock} disabled={busy}>
              lock
            </button>
          </div>
          <SectionBody section={section} {...sectionProps} />
        </>
      )}
    </div>
  );
}

/** The section switch body — all four control-plane sections are live (17-06 full coverage, D-16). */
function SectionBody({ section, ...props }: { section: Section } & ControlSectionProps) {
  switch (section) {
    case 'generate':
      return <GenerateForm {...props} />;
    case 'analyze':
      return <AnalyzeForm {...props} />;
    case 'tenants':
      return <TenantsManager {...props} />;
    case 'server-status':
      return <ServerStatusView />;
    default:
      return null;
  }
}
