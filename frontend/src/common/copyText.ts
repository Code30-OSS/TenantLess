/**
 * Copy to clipboard with a secure-context primary path (`navigator.clipboard.writeText`) and a
 * legacy `document.execCommand('copy')` fallback (RESEARCH Pitfall 4 — the Clipboard API needs a
 * secure context; loopback/HTTPS count, a plain-HTTP non-loopback origin would block it).
 *
 * Promoted from `explorer/JsonTree.tsx` so every consumer reuses one hardened helper. Resolves `true`
 * only when a copy path actually succeeded and `false` otherwise, so a caller never renders a false
 * "Copied". Crucially the primary path is AWAITED: if `writeText` REJECTS (permission/browser policy)
 * we fall through to the legacy path instead of swallowing the rejection as success (UAT P2). Copy is
 * best-effort: a fully-blocked clipboard resolves `false` and NEVER throws or leaks an unhandled
 * rejection into the render tree (T-18-05).
 */
export async function copyText(text: string): Promise<boolean> {
  const clip = navigator.clipboard;
  if (clip && typeof clip.writeText === 'function') {
    try {
      await clip.writeText(text);
      return true;
    } catch {
      // Clipboard API present but rejected (permission/policy) — fall through to the legacy path
      // rather than reporting a success that did not happen.
    }
  }
  return legacyCopy(text);
}

/** Legacy `execCommand('copy')` path. Returns whether the copy actually succeeded; never throws. */
function legacyCopy(text: string): boolean {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok === true;
  } catch {
    // Both paths blocked — report failure so the UI can avoid a false "Copied".
    return false;
  }
}
