/**
 * OverviewView (S0 — DEMO front door, 18-UI-SPEC §S0) — the lightweight `/ui/overview` landing.
 *
 * The console's front door (D-04): a verbatim S0 head band (eyebrow `SCANNER-FACING DEMO`, H1
 * `Point a scanner at this tenant.`, the scanner-facing subtitle) over four orientation cards that
 * link out to the existing sections (Explorer / Console / Control Plane / Scanner Demo). It mirrors
 * the `tenantless-landing.html` `.uses` card grid + the ComingSoon centered-card idiom, but stays
 * lightweight — no marketing hero, no terminal animation (those live on the public landing page).
 *
 * Static only — no fetch, no loading/error state. Links are in-app react-router root-relative paths
 * (never absolute/user-supplied), so there is no open-redirect surface (T-18-09). Styling is
 * tokens-only (no raw hex, sharp corners) in OverviewView.module.css.
 */
import { Link } from 'react-router';

import styles from './OverviewView.module.css';

interface OrientationCard {
  /** Bold lead label + the in-app target path it links to. */
  lead: string;
  to: string;
  description: string;
}

/** The four orientation cards — verbatim copy from 18-UI-SPEC §S0. */
const CARDS: OrientationCard[] = [
  {
    lead: 'Explorer',
    to: '/explorer/resources',
    description: 'Browse the synthetic subscriptions, resource groups, and resources.',
  },
  {
    lead: 'Console',
    to: '/console',
    description: 'Watch live request latency and drill into routes and status codes.',
  },
  {
    lead: 'Control Plane',
    to: '/control-plane/generate',
    description: 'Generate, analyze, and manage synthetic tenants.',
  },
  {
    lead: 'Scanner Demo',
    to: '/demo/catalog',
    description: 'Endpoint catalog, scanner config, and a live ARM response viewer.',
  },
];

export default function OverviewView() {
  return (
    <section className={styles.view} aria-label="Overview">
      <header className={styles.head}>
        <div className={styles.eyebrow}>◆ SCANNER-FACING DEMO</div>
        <h1 className={styles.title}>Point a scanner at this tenant.</h1>
        <p className={styles.subtitle}>
          This is a synthetic Azure tenant served on an ARM-compatible API. Browse what a scanner
          sees, copy the config to point your own tooling here, and run live ARM requests against the
          current tenant.
        </p>
      </header>

      <div className={styles.cards}>
        {CARDS.map((card) => (
          <Link key={card.to} to={card.to} className={styles.card}>
            <span className={styles.mark} aria-hidden="true">
              ◆
            </span>
            <span className={styles.cardLead}>{card.lead}</span>
            <span className={styles.cardDescription}>{card.description}</span>
          </Link>
        ))}
      </div>
    </section>
  );
}
