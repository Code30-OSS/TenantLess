/**
 * FilterBar — the EXPL-05 server-side `$filter` CONTROLS over the ARM resource-list route.
 *
 * Filtering is SERVER-SIDE (D-04) — the ~102K resource set is NEVER loaded or filtered client-side.
 * Guided pickers (resourceType / location / tag key+value) compose an OData `$filter` string via
 * `buildFilter` (15-01); a raw `$filter` input lets power users type OData directly (and, when present,
 * takes precedence).
 *
 * Post-Miller (15-16) this bar is CONTROLS-ONLY: it no longer renders its own result list (that would
 * duplicate the Miller col2 {@link ResourceColumn}, which is now the single place the selected RG's
 * resources render). On apply it lifts the composed `$filter` up via `onApply`; the view threads it
 * into col2, so the filter NARROWS the middle column and col2 owns the list, pager, and the ARM-400
 * fail-closed message.
 *
 * Filters apply DELIBERATELY (UAT Gap 6 / 15-12): the field inputs bind to a DRAFT; the composed
 * `$filter` lifted via `onApply` derives from the APPLIED snapshot, committed only on an explicit
 * apply (Apply button / Enter / blur). Typing therefore lifts NOTHING per keystroke.
 */
import { useState } from 'react';

import { buildFilter, type FilterClause } from '../api/odata';
import styles from './FilterBar.module.css';

interface FilterBarProps {
  /** The active subscription scope (from the view's URL params), or null. */
  sub: string | null;
  /** The active resource-group scope, or null — the filter narrows the selected RG's col2 list. */
  rg: string | null;
  /**
   * Raised on an explicit apply/clear with the composed OData `$filter` (or undefined when cleared).
   * The view stores it and threads it into col2 ({@link ResourceColumn}) — filtering happens THERE.
   */
  onApply: (filter: string | undefined) => void;
}

/** The set of filter fields, carried both as live draft state and as the committed snapshot. */
interface FilterDraft {
  resourceType: string;
  location: string;
  tagName: string;
  tagValue: string;
  raw: string;
}

const EMPTY: FilterDraft = { resourceType: '', location: '', tagName: '', tagValue: '', raw: '' };

export default function FilterBar({ sub, rg, onApply }: FilterBarProps) {
  // DRAFT input state — typing updates ONLY the draft; it never lifts up (UAT Gap 6).
  const [resourceType, setResourceType] = useState('');
  const [location, setLocation] = useState('');
  const [tagName, setTagName] = useState('');
  const [tagValue, setTagValue] = useState('');
  const [raw, setRaw] = useState('');

  // APPLIED snapshot — the committed filter, mirrored up via onApply. Drives `dirty` + the Clear affordance.
  const [applied, setApplied] = useState<FilterDraft>(EMPTY);

  const hasFilter = compose(applied) !== undefined;

  // Whether the live draft differs from what is applied (enables the Apply affordance).
  const dirty =
    resourceType !== applied.resourceType ||
    location !== applied.location ||
    tagName !== applied.tagName ||
    tagValue !== applied.tagValue ||
    raw !== applied.raw;

  // Commit the draft -> applied snapshot and lift the composed $filter to the view (which feeds col2).
  function apply() {
    const next: FilterDraft = { resourceType, location, tagName, tagValue, raw };
    setApplied(next);
    onApply(compose(next));
  }

  function clear() {
    setResourceType('');
    setLocation('');
    setTagName('');
    setTagValue('');
    setRaw('');
    setApplied(EMPTY);
    onApply(undefined);
  }

  const scoped = Boolean(sub) && Boolean(rg);

  return (
    <div className={styles.bar}>
      <div className={styles.controls}>
        <Field label="resourceType" value={resourceType} onChange={setResourceType} onCommit={apply} placeholder="Microsoft.Storage/storageAccounts" />
        <Field label="location" value={location} onChange={setLocation} onCommit={apply} placeholder="westeurope" />
        <Field label="tagName" value={tagName} onChange={setTagName} onCommit={apply} placeholder="env" />
        <Field label="tagValue" value={tagValue} onChange={setTagValue} onCommit={apply} placeholder="prod" />
        <label className={styles.rawField}>
          <span className={styles.label}>$filter</span>
          <input
            className={styles.rawInput}
            aria-label="$filter"
            value={raw}
            placeholder="resourceType eq 'Microsoft.Storage/storageAccounts'"
            onChange={(e) => setRaw(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') apply();
            }}
            onBlur={apply}
          />
        </label>
        <button type="button" className={styles.clear} onClick={apply} disabled={!dirty}>
          Apply filter
        </button>
        {hasFilter && (
          <button type="button" className={styles.clear} onClick={clear}>
            Clear filter
          </button>
        )}
      </div>

      {!scoped && (
        <p className={styles.hint}>Select a resource group in the tree to filter its resources.</p>
      )}
    </div>
  );
}

/** Compose an applied draft into an OData `$filter` (raw wins), or undefined when empty. */
function compose(fields: FilterDraft): string | undefined {
  const composed = fields.raw.trim() !== '' ? fields.raw.trim() : buildGuided(fields);
  return composed !== '' ? composed : undefined;
}

/** Compose the guided pickers into an OData `$filter` via the tested composer (empty → ""). */
function buildGuided(fields: {
  resourceType: string;
  location: string;
  tagName: string;
  tagValue: string;
}): string {
  const clauses: FilterClause[] = [];
  if (fields.resourceType.trim()) clauses.push({ field: 'resourceType', value: fields.resourceType.trim() });
  if (fields.location.trim()) clauses.push({ field: 'location', value: fields.location.trim() });
  if (fields.tagName.trim()) clauses.push({ field: 'tagName', value: fields.tagName.trim() });
  if (fields.tagValue.trim()) clauses.push({ field: 'tagValue', value: fields.tagValue.trim() });
  return buildFilter(clauses);
}

interface FieldProps {
  label: string;
  value: string;
  onChange: (v: string) => void;
  /** Commit the draft to the applied filter (Enter / blur). */
  onCommit: () => void;
  placeholder: string;
}

function Field({ label, value, onChange, onCommit, placeholder }: FieldProps) {
  return (
    <label className={styles.field}>
      <span className={styles.label}>{label}</span>
      <input
        className={styles.input}
        aria-label={label}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') onCommit();
        }}
        onBlur={onCommit}
      />
    </label>
  );
}
