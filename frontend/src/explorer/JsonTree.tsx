/**
 * JsonTree — the hand-rolled, recursive collapsible JSON tree for arbitrary-depth ARM `properties`
 * (EXPL-02, D-03). Deliberately NOT a JSON-viewer dependency (D-07 minimal footprint) — the
 * copy-path / typed-leaf behavior is ~a screenful here and the design is pixel-locked (Space Mono
 * 11.5px / line-height 1.85, tokens only).
 *
 * A `Node` renders a branch (object/array) with an expand/collapse toggle and recurses over its
 * entries, or a typed `Leaf`. Every node is keyed by its dotted `path` (never an array index alone)
 * so React state (open/closed) and copy targets stay stable across re-renders. An empty object
 * renders a muted "No properties" note (MOCK-13 guarantees an object, never null — no crash).
 */
import { useState } from 'react';

import { copyText } from '../common/copyText';
import styles from './JsonTree.module.css';

/**
 * Belt-and-suspenders recursion bound (UAT Gap 3 / D-03). Normal ARM `properties` are far shallower;
 * this only guards against a pathological synthetic blob so a fully-expanded branch cannot exhaust the
 * call stack. At/beyond this depth a branch renders a muted note instead of recursing further.
 */
const MAX_DEPTH = 40;

interface JsonTreeProps {
  data: unknown;
  /** Label for the root node; defaults to `properties` (the ARM detail block this hosts). */
  rootLabel?: string;
  /**
   * Branches at a depth `< initialOpenDepth` start expanded; deeper ones start collapsed. Defaults to
   * `1` — root open, descendants collapsed (the live Explorer's lazy-mount posture, UAT Gap 3). The S1
   * demo catalog passes a large value so its small, curated samples read fully expanded at a glance.
   */
  initialOpenDepth?: number;
  /**
   * When true and `data` is a non-empty branch, render its entries directly with no enclosing root
   * node, so a response envelope like `{value:[…]}` reads as `value [ … ]` rather than a redundant
   * `rootLabel { value [ … ] }` wrapper. Used by the demo catalog (the sample IS the full response).
   */
  hideRoot?: boolean;
}

export default function JsonTree({
  data,
  rootLabel = 'properties',
  initialOpenDepth = 1,
  hideRoot = false,
}: JsonTreeProps) {
  const rootless = hideRoot && isBranch(data) && entriesOf(data).length > 0;
  return (
    <div className={styles.tree} role="tree">
      {rootless ? (
        entriesOf(data).map(([ck, cv]) => (
          <Node key={ck} k={ck} value={cv} path={ck} depth={0} initialOpenDepth={initialOpenDepth} />
        ))
      ) : (
        <Node k={rootLabel} value={data} path={rootLabel} depth={0} initialOpenDepth={initialOpenDepth} />
      )}
    </div>
  );
}

interface NodeProps {
  k: string;
  value: unknown;
  path: string;
  depth: number;
  initialOpenDepth: number;
}

function isBranch(value: unknown): value is object {
  return value !== null && typeof value === 'object';
}

function entriesOf(value: object): [string, unknown][] {
  return Array.isArray(value)
    ? value.map((v, i) => [String(i), v] as [string, unknown])
    : Object.entries(value as Record<string, unknown>);
}

function Node({ k, value, path, depth, initialOpenDepth }: NodeProps) {
  // Branches shallower than `initialOpenDepth` open by default; deeper ones start collapsed so
  // descendants mount lazily on expand (the `{open && !empty && <children/>}` gate below). The default
  // `initialOpenDepth = 1` restores the D-03 collapsible intent and keeps a large/deeply nested
  // `properties` blob from mounting eagerly on first paint (UAT Gap 3); the demo catalog raises it so
  // its small curated samples render fully expanded.
  const [open, setOpen] = useState(depth < initialOpenDepth);

  if (!isBranch(value)) {
    return <Leaf k={k} value={value} path={path} />;
  }

  const isArray = Array.isArray(value);
  const entries = entriesOf(value);
  const empty = entries.length === 0;
  // Hard recursion bound: at/beyond MAX_DEPTH a branch stops recursing even when expanded.
  const capped = depth >= MAX_DEPTH && !empty;

  return (
    <div className={styles.node} role="treeitem" aria-expanded={open}>
      <div className={styles.branchRow} style={{ paddingLeft: `${depth * 14}px` }}>
        <button
          type="button"
          className={styles.toggle}
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          aria-label={`Toggle ${k}`}
        >
          {open ? '▾' : '▸'}
        </button>
        <span className={styles.key}>{k}</span>
        <span className={styles.brace}>{isArray ? '[' : '{'}</span>
        {empty && <span className={styles.brace}>{isArray ? ']' : '}'}</span>}
        {empty && !isArray && <span className={styles.note}>No properties</span>}
        <CopyButtons path={path} value={value} />
      </div>
      {open && capped && (
        <div className={styles.children}>
          <span className={styles.note} style={{ paddingLeft: `${(depth + 1) * 14}px` }}>
            … (nesting depth capped)
          </span>
        </div>
      )}
      {open && !empty && !capped && (
        <div className={styles.children}>
          {entries.map(([ck, cv]) => (
            <Node
              key={`${path}.${ck}`}
              k={ck}
              value={cv}
              path={`${path}.${ck}`}
              depth={depth + 1}
              initialOpenDepth={initialOpenDepth}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function Leaf({ k, value, path }: { k: string; value: unknown; path: string }) {
  return (
    <div className={styles.leafRow} role="treeitem">
      <span className={styles.key}>{k}</span>
      <span className={styles.colon}>:</span>
      <span className={styles.value} data-type={typeOf(value)}>
        {formatValue(value)}
      </span>
      <CopyButtons path={path} value={value} />
    </div>
  );
}

function CopyButtons({ path, value }: { path: string; value: unknown }) {
  return (
    <span className={styles.copies}>
      <button
        type="button"
        className={styles.copyBtn}
        onClick={() => {
          void copyText(path);
        }}
        aria-label={`Copy path ${path}`}
      >
        Copy path
      </button>
      <button
        type="button"
        className={styles.copyBtn}
        onClick={() => {
          void copyText(JSON.stringify(value));
        }}
        aria-label={`Copy value ${path}`}
      >
        Copy value
      </button>
    </span>
  );
}

/** Type discriminator for the leaf value (drives the `data-type` color + the test contract). */
function typeOf(value: unknown): 'string' | 'number' | 'boolean' | 'null' | 'undefined' {
  if (value === null) return 'null';
  if (value === undefined) return 'undefined';
  const t = typeof value;
  return t === 'string' || t === 'number' || t === 'boolean' ? t : 'string';
}

/** Render a leaf value: strings quoted, everything else as its literal source form. */
function formatValue(value: unknown): string {
  if (value === null) return 'null';
  if (value === undefined) return 'undefined';
  if (typeof value === 'string') return `"${value}"`;
  return String(value);
}
