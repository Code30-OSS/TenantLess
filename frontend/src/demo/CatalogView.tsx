/**
 * CatalogView (S1 — DEMO-01, 18-UI-SPEC §S1) — a scannable index of the headline ARM discovery routes.
 *
 * The "teach the contract" half of the scanner demo (D-01): it renders the curated 18-01 {@link CATALOG}
 * data — five capability groups, each a stack of rows showing an HTTP method chip + route template +
 * api-version + one-line purpose. Every canned sample sits behind a `Sample response` disclosure that
 * LAZILY mounts the reused {@link JsonTree} on expand (D-01/D-01a), so the catalog reads as an index first
 * and only materialises a tree when asked. It is fully static — no fetch (D-01b); the live "try it" surface
 * is the DEMO-03 viewer.
 *
 * Composition-only: the head band mirrors the ResourcesView eyebrow/title idiom and the sample block reuses
 * JsonTree as-is (already opens depth 0 only — ideal for a collapsed catalog; `rootLabel` per entry, Pitfall 8).
 * Styling is tokens-only (no raw hex, sharp corners) in CatalogView.module.css.
 */
import { useState } from 'react';

import JsonTree from '../explorer/JsonTree';
import { CATALOG, type CatalogEntry } from './catalog';
import styles from './CatalogView.module.css';

export default function CatalogView() {
  return (
    <section className={styles.view} aria-label="Endpoint catalog">
      <header className={styles.head}>
        <div className={styles.eyebrow}>◆ ENDPOINT CATALOG</div>
        <h1 className={styles.title}>What an ARM scanner sees</h1>
        <p className={styles.subtitle}>
          A curated tour of the headline ARM discovery routes this simulator serves, grouped by
          capability. Each sample response is a static illustration of the response shape — to run one
          live against the current tenant, use the response viewer.
        </p>
        <p className={styles.microNote}>Static samples — illustrative shapes, not fetched live.</p>
      </header>

      {CATALOG.map((group) => (
        <div key={group.title} className={styles.group}>
          <div className={styles.groupHeading}>{group.title}</div>
          <div className={styles.rows}>
            {group.entries.map((entry) => (
              <CatalogRow key={`${entry.method} ${entry.route}`} entry={entry} />
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}

/**
 * One catalog row: method chip + route + api-version + purpose, plus a `Sample response` disclosure.
 * The JsonTree is gated on `open` so it is NOT in the DOM until the disclosure is expanded (lazy mount).
 */
function CatalogRow({ entry }: { entry: CatalogEntry }) {
  const [open, setOpen] = useState(false);

  return (
    <div className={styles.row}>
      <div className={styles.routeLine}>
        <span className={styles.method} data-method={entry.method}>
          {entry.method}
        </span>
        <span className={styles.route}>{entry.route}</span>
        {entry.apiVersion && <span className={styles.apiVersion}>{entry.apiVersion}</span>}
      </div>
      <div className={styles.purpose}>{entry.purpose}</div>
      <button
        type="button"
        className={styles.disclosure}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className={styles.caret} aria-hidden="true">
          {open ? '▾' : '▸'}
        </span>
        Sample response
      </button>
      {open && (
        <div className={styles.sample}>
          {/*
            The sample IS the full curated response envelope, so render it rootless (no redundant
            `rootLabel { … }` wrapper — a `{value:[…]}` sample reads as `value [ … ]`) and fully
            expanded: these are small, static shapes meant to be read at a glance, unlike the live
            Explorer tree which keeps its lazy collapse. (Ph18 UAT gap fix.)
          */}
          <JsonTree data={entry.sample} hideRoot initialOpenDepth={Number.POSITIVE_INFINITY} />
        </div>
      )}
    </div>
  );
}
