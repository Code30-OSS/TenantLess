/**
 * ResourceDetail — the Explorer right-column panel (EXPL-02). Given a selected ARM id it fetches the
 * full ARM detail via `useResourceDetail` and renders: a header (type eyebrow / name), the full ARM
 * `id` (Space Mono, wrapped), a meta k/v list, a `◆ properties` block hosting the collapsible
 * {@link JsonTree}, and a `◆ nested resources` block when the detail carries child resources.
 *
 * States (UI-SPEC EXPL-02): no-selection placeholder, `pulse` loading skeleton, an error panel with
 * Retry, and the populated detail. The `◆ Governance violations` block (EXPL-03) is inserted by the
 * 15-04 Task-3 wiring (`ViolationsBlock`) — it fetches independently so its own error never blanks
 * the JSON tree.
 */
import { useResourceDetail } from '../api/queries';
import JsonTree from './JsonTree';
import { ViolationsBlock } from './ViolationChip';
import styles from './ResourceDetail.module.css';

interface ResourceDetailProps {
  /** The selected resource's full ARM id, or null when nothing is selected. */
  armId: string | null;
}

export default function ResourceDetail({ armId }: ResourceDetailProps) {
  const { data, isLoading, isError, refetch } = useResourceDetail(armId);

  if (!armId) {
    return (
      <div className={styles.panel}>
        <p className={styles.emptyState}>Select a resource to inspect its ARM properties.</p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className={styles.panel}>
        <div className={styles.skeleton} aria-label="Loading resource detail" />
        <div className={styles.skeleton} />
        <div className={styles.skeleton} />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className={styles.panel}>
        <p className={styles.errorState}>Could not load resource detail.</p>
        <button type="button" className={styles.retry} onClick={() => void refetch()}>
          Retry
        </button>
      </div>
    );
  }

  const nested = data.resources ?? [];

  return (
    <div className={styles.panel}>
      <header className={styles.header}>
        <div className={styles.eyebrow}>{data.type}</div>
        <h2 className={styles.name}>{data.name}</h2>
      </header>

      <div className={styles.armId}>{data.id}</div>

      <dl className={styles.meta}>
        <MetaRow label="location" value={data.location} />
        {data.kind && <MetaRow label="kind" value={data.kind} />}
        {data.tags &&
          Object.entries(data.tags).map(([key, value]) => (
            <MetaRow key={`tag-${key}`} label={`tag:${key}`} value={value} />
          ))}
      </dl>

      <ViolationsBlock resource={armId} />

      <section className={styles.block}>
        <div className={styles.blockTitle}>◆ properties</div>
        <JsonTree data={data.properties} />
      </section>

      {nested.length > 0 && (
        <section className={styles.block}>
          <div className={styles.blockTitle}>◆ nested resources</div>
          <ul className={styles.nestedList}>
            {nested.map((child) => (
              <li key={child.id} className={styles.nestedItem}>
                <span className={styles.nestedName}>{child.name}</span>
                <span className={styles.nestedType}>{child.type}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function MetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.metaRow}>
      <dt className={styles.metaKey}>{label}</dt>
      <dd className={styles.metaValue}>{value}</dd>
    </div>
  );
}
