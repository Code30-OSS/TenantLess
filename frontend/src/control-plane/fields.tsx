/**
 * Field primitives for the control-plane forms (Phase 17, 17-UI-SPEC §Accessibility).
 *
 * Thin, presentational wrappers over the canonical FilterBar `.field/.label/.input` idiom with an
 * inline validation slot and full a11y: every control has a `<label htmlFor>`; validation hints are
 * linked via `aria-describedby`; invalid fields set `aria-invalid`; the slider is a native
 * `<input type="range">` with `aria-valuetext`; the toggle is a real `<button role="switch">`. All
 * styling lives in `controls.module.css` (tokens only, sharp corners) — no inline colors.
 */
import type { ButtonHTMLAttributes, ReactNode } from 'react';

import styles from './controls.module.css';

/** The shared describedby id: the error slot when invalid, else the hint slot, else none. */
function describedBy(id: string, error?: string | null, hint?: ReactNode): string | undefined {
  if (error) return `${id}-error`;
  if (hint) return `${id}-hint`;
  return undefined;
}

/** The error-or-hint footnote rendered under a field (error wins; both carry a stable id). */
function FieldNote({ id, error, hint }: { id: string; error?: string | null; hint?: ReactNode }) {
  if (error) {
    return (
      <span id={`${id}-error`} className={styles.errorText}>
        {error}
      </span>
    );
  }
  if (hint) {
    return (
      <span id={`${id}-hint`} className={styles.hint}>
        {hint}
      </span>
    );
  }
  return null;
}

export interface NumberFieldProps {
  id: string;
  label: string;
  /** The controlled raw value — a string (the operator's in-progress text) or a number. */
  value: string | number;
  onChange: (raw: string) => void;
  onBlur?: () => void;
  min?: number;
  max?: number;
  disabled?: boolean;
  error?: string | null;
  hint?: ReactNode;
}

/** A numeric field (client caps are advisory UX — the server re-validates, T-17-01). */
export function NumberField({
  id,
  label,
  value,
  onChange,
  onBlur,
  min,
  max,
  disabled,
  error,
  hint,
}: NumberFieldProps) {
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className={error ? `${styles.input} ${styles.inputError}` : styles.input}
        type="number"
        inputMode="numeric"
        value={value}
        min={min}
        max={max}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, error, hint)}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onBlur}
      />
      <FieldNote id={id} error={error} hint={hint} />
    </div>
  );
}

export interface TextFieldProps {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  onBlur?: () => void;
  type?: 'text' | 'password';
  autoComplete?: string;
  placeholder?: string;
  disabled?: boolean;
  error?: string | null;
  hint?: ReactNode;
}

/** A text field. `type="password"` + `autoComplete="off"` is the token-gate posture (T-17-05). */
export function TextField({
  id,
  label,
  value,
  onChange,
  onBlur,
  type = 'text',
  autoComplete,
  placeholder,
  disabled,
  error,
  hint,
}: TextFieldProps) {
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        className={error ? `${styles.input} ${styles.inputError}` : styles.input}
        type={type}
        autoComplete={autoComplete}
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, error, hint)}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onBlur}
      />
      <FieldNote id={id} error={error} hint={hint} />
    </div>
  );
}

export interface SelectOption {
  value: string;
  label: string;
}

export interface SelectFieldProps {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: SelectOption[];
  placeholder?: string;
  disabled?: boolean;
  error?: string | null;
  hint?: ReactNode;
}

/** A server-populated `<select>` (allowlist, never a free path input — T-17-01/D-12). */
export function SelectField({
  id,
  label,
  value,
  onChange,
  options,
  placeholder,
  disabled,
  error,
  hint,
}: SelectFieldProps) {
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
      </label>
      <select
        id={id}
        className={styles.select}
        value={value}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy(id, error, hint)}
        onChange={(e) => onChange(e.target.value)}
      >
        {placeholder !== undefined && (
          <option value="" disabled>
            {placeholder}
          </option>
        )}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <FieldNote id={id} error={error} hint={hint} />
    </div>
  );
}

export interface ViolationRateSliderProps {
  id: string;
  label: string;
  value: number;
  onChange: (v: number) => void;
  disabled?: boolean;
  hint?: ReactNode;
}

/**
 * The governance-violation slider. Value `> 0` ⇒ `--violations` (on), `0` ⇒ `--no-violations` (D-08):
 * the percentage is a UX affordance, NOT a server-side rate. Native range → `aria-valuetext="{n}%"`.
 */
export function ViolationRateSlider({
  id,
  label,
  value,
  onChange,
  disabled,
  hint,
}: ViolationRateSliderProps) {
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
      </label>
      <div className={styles.sliderRow}>
        <input
          id={id}
          className={styles.slider}
          type="range"
          min={0}
          max={100}
          value={value}
          disabled={disabled}
          aria-valuetext={`${value}%`}
          aria-describedby={hint ? `${id}-hint` : undefined}
          onChange={(e) => onChange(Number(e.target.value))}
        />
        <span className={styles.sliderReadout} aria-hidden="true">
          {value}%
        </span>
      </div>
      {hint && (
        <span id={`${id}-hint`} className={styles.hint}>
          {hint}
        </span>
      )}
    </div>
  );
}

export interface ToggleSwitchProps {
  id: string;
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}

/** A real `<button role="switch" aria-checked>` — not a bare div (a11y contract). */
export function ToggleSwitch({ id, label, checked, onChange, disabled }: ToggleSwitchProps) {
  return (
    <div className={styles.toggleField}>
      <span className={styles.label} id={`${id}-label`}>
        {label}
      </span>
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={checked}
        aria-labelledby={`${id}-label`}
        disabled={disabled}
        className={styles.toggle}
        onClick={() => onChange(!checked)}
      >
        <span className={styles.toggleKnob} aria-hidden="true" />
      </button>
    </div>
  );
}

/** The single gold primary CTA per screen (`type="button"` default; caller may override). */
export function PrimaryButton(props: ButtonHTMLAttributes<HTMLButtonElement>) {
  const { className: _ignored, ...rest } = props;
  return <button type="button" className={styles.primaryBtn} {...rest} />;
}

/** The outlined secondary button. */
export function SecondaryButton(props: ButtonHTMLAttributes<HTMLButtonElement>) {
  const { className: _ignored, ...rest } = props;
  return <button type="button" className={styles.secondaryBtn} {...rest} />;
}
