/**
 * ScannerConfigView (S2 — DEMO-02, 18-UI-SPEC §S2) — the "point your scanner here" config view.
 *
 * The paste-ready half of the scanner demo (D-02): three copyable generic value rows (origin-derived
 * Base URL, a representative api-version, the any-Bearer Authorization value + explainer) plus two
 * copyable ready-to-run snippet blocks (a curl one-liner and a generic static-token scanner env block).
 * Every value/snippet has a `Copy → Copied` flip affordance backed by the promoted, never-throws
 * {@link copyText} helper (18-02), with a polite aria-live announcer.
 *
 * Composition-only: all strings come from the tested 18-02 {@link ./config} builders — the origin is
 * read through `baseUrl()` (`window.location.origin`), NEVER hardcoded — and the copy path reuses
 * `common/copyText`. D-05 (locked, AUTHORITATIVE over the UI-SPEC's brand-named env copy): the env
 * block ships the generic `SCANNER_ARM_ENDPOINT` / `SCANNER_STATIC_TOKEN` names under a neutral
 * `STATIC-TOKEN SCANNER (PATH A)` heading — ZERO forbidden brand token anywhere. This page teaches;
 * it does not fetch (no token entry, no live call — that is the DEMO-03 viewer).
 */
import { useRef, useState } from 'react';

import { copyText } from '../common/copyText';
import {
  API_VERSION,
  AUTHORIZATION_EXPLAINER,
  AUTHORIZATION_VALUE,
  ENV_BODY_NOTE,
  SCANNER_ENV_HEADING,
  baseUrl,
  curlSnippet,
  envSnippet,
} from './config';
import styles from './ScannerConfigView.module.css';

/** How long the `Copy → Copied` flip persists before reverting (UI-SPEC S2 ~1.5s). */
const COPIED_MS = 1500;

export default function ScannerConfigView() {
  // Live origin via config.ts (window.location.origin) — never hardcoded (D-02a / T-18-06).
  const origin = baseUrl();

  return (
    <section className={styles.view} aria-label="Scanner configuration">
      <header className={styles.head}>
        <div className={styles.eyebrow}>◆ POINT YOUR SCANNER HERE</div>
        <h1 className={styles.title}>Scanner configuration</h1>
        <p className={styles.subtitle}>
          Everything a scanner needs to treat this simulator like a real Azure tenant. Base URL is
          detected from this page&apos;s origin — copy the values or the ready-to-run snippets below.
        </p>
      </header>

      <div className={styles.panel}>
        <ValueRow label="BASE URL" value={origin} />
        <ValueRow label="API-VERSION" value={API_VERSION} />
        <ValueRow
          label="AUTHORIZATION"
          value={AUTHORIZATION_VALUE}
          note={AUTHORIZATION_EXPLAINER}
        />
      </div>

      <SnippetBlock heading="CURL" body={curlSnippet(origin)} />
      <SnippetBlock heading={SCANNER_ENV_HEADING} body={envSnippet(origin)} note={ENV_BODY_NOTE} />
    </section>
  );
}

/** One copyable generic value row: micro label + Copy button + the mono value on a `--canvas` inset. */
function ValueRow({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className={styles.field}>
      <div className={styles.rowHead}>
        <span className={styles.label}>{label}</span>
        <CopyButton text={value} />
      </div>
      <code className={styles.value}>{value}</code>
      {note && <p className={styles.explainer}>{note}</p>}
    </div>
  );
}

/** One copyable ready-to-run snippet block: mono heading + Copy button + a `pre-wrap` code inset. */
function SnippetBlock({
  heading,
  body,
  note,
}: {
  heading: string;
  body: string;
  note?: string;
}) {
  return (
    <div className={styles.snippet}>
      <div className={styles.rowHead}>
        <span className={styles.snippetHeading}>{heading}</span>
        <CopyButton text={body} />
      </div>
      <pre className={styles.code}>{body}</pre>
      {note && <p className={styles.explainer}>{note}</p>}
    </div>
  );
}

/**
 * A `Copy → Copied` button holding its own 1500ms flip state, plus a visually-hidden polite live
 * region that announces the copy. It awaits {@link copyText} and reflects the REAL result — flipping
 * to `Copied` only on a genuine success and to `Copy failed` when the clipboard is blocked/denied, so
 * the operator is never told a value was copied when it was not (UAT P2). Copy is best-effort:
 * `copyText` never throws into render and resolves `false` rather than rejecting (T-18-05).
 */
function CopyButton({ text }: { text: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  async function onCopy() {
    const ok = await copyText(text);
    if (timer.current) clearTimeout(timer.current);
    setState(ok ? 'copied' : 'failed');
    timer.current = setTimeout(() => setState('idle'), COPIED_MS);
  }

  const label = state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy';
  const announcement =
    state === 'copied'
      ? 'Copied to clipboard'
      : state === 'failed'
        ? 'Copy to clipboard failed'
        : '';

  return (
    <>
      <button
        type="button"
        className={state === 'copied' ? `${styles.copyBtn} ${styles.copyBtnDone}` : styles.copyBtn}
        onClick={() => {
          void onCopy();
        }}
      >
        {label}
      </button>
      <span className={styles.srOnly} role="status" aria-live="polite">
        {announcement}
      </span>
    </>
  );
}
