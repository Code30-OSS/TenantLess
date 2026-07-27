/**
 * ComingSoon — the shared placeholder stub for the three not-yet-built sections (WEBUI-04).
 *
 * One component parameterized by `section`; 15-03 wires it into the Console / Control Plane / Demo
 * stub routes. Copy is the verbatim Placeholder Contract from 15-UI-SPEC.md (H1 + subtitle + a
 * phase-tag micro-label). Static only — no fetch, no loading/error state.
 */
import styles from './ComingSoon.module.css';

export type ComingSoonSection = 'console' | 'control-plane' | 'demo';

interface SectionCopy {
  title: string;
  subtitle: string;
  phase: string;
}

const SECTION_COPY: Record<ComingSoonSection, SectionCopy> = {
  console: {
    title: 'Observability Console',
    subtitle:
      'Latency history, route & status drill-down, and a request inspector land in a later phase.',
    phase: 'PHASE 16',
  },
  'control-plane': {
    title: 'Control Plane',
    subtitle:
      'Trigger analyzer & generator runs, track jobs, and manage tenants — coming in a later phase.',
    phase: 'PHASE 17',
  },
  demo: {
    title: 'Scanner Demo',
    subtitle:
      'Endpoint catalog, scanner config, and a live ARM response viewer — coming in a later phase.',
    phase: 'PHASE 18',
  },
};

export default function ComingSoon({ section }: { section: ComingSoonSection }) {
  const copy = SECTION_COPY[section];
  return (
    <div className={styles.wrap}>
      <div className={styles.mark} aria-hidden="true">
        ◆
      </div>
      <h1 className={styles.title}>{copy.title}</h1>
      <p className={styles.subtitle}>{copy.subtitle}</p>
      <div className={styles.phase}>{copy.phase}</div>
    </div>
  );
}
