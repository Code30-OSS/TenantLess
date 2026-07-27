import { afterEach, describe, expect, it, vi } from 'vitest';

import { copyText } from './copyText';

/**
 * Contract for the promoted clipboard helper (extracted from JsonTree so the S2 view (18-05) reuses
 * it instead of re-inventing). Guarantees:
 *   - primary path: `navigator.clipboard.writeText` is used when present, and its result is AWAITED —
 *     a rejection (permission/policy) must NOT be swallowed as success (UAT P2);
 *   - fallback path: `document.execCommand('copy')` is used when the Clipboard API is absent OR when
 *     `writeText` rejects (insecure/non-loopback context — RESEARCH Pitfall 4);
 *   - honest result: resolves `true` only when a copy path actually succeeded, `false` otherwise, so a
 *     caller never renders a false "Copied";
 *   - never-throws: a blocked clipboard (both paths failing) resolves `false` and never throws or
 *     leaks an unhandled rejection into the render tree (T-18-05 — copy is a best-effort affordance).
 */
describe('copyText — clipboard with execCommand fallback + honest success (T-18-05, UAT P2)', () => {
  // jsdom does not define document.execCommand, so it is installed per-case and removed after.
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    delete (document as unknown as { execCommand?: unknown }).execCommand;
  });

  function setExecCommand(impl: () => boolean): ReturnType<typeof vi.fn> {
    const exec = vi.fn(impl);
    (document as unknown as { execCommand: unknown }).execCommand = exec;
    return exec;
  }

  it('uses navigator.clipboard.writeText and resolves true on success (primary path)', async () => {
    const writeText = vi.fn(() => Promise.resolve());
    vi.stubGlobal('navigator', { clipboard: { writeText } });

    await expect(copyText('primary payload')).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith('primary payload');
  });

  it('falls back to document.execCommand("copy") and resolves true when the Clipboard API is absent', async () => {
    vi.stubGlobal('navigator', {});
    const exec = setExecCommand(() => true);

    await expect(copyText('fallback payload')).resolves.toBe(true);
    expect(exec).toHaveBeenCalledWith('copy');
  });

  it('falls back to execCommand when writeText REJECTS (permission/policy), not a false success', async () => {
    const writeText = vi.fn(() => Promise.reject(new Error('permission denied')));
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const exec = setExecCommand(() => true);

    await expect(copyText('rejected payload')).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith('rejected payload');
    expect(exec).toHaveBeenCalledWith('copy');
  });

  it('resolves false (no throw, no unhandled rejection) when writeText rejects AND execCommand fails', async () => {
    const writeText = vi.fn(() => Promise.reject(new Error('permission denied')));
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    setExecCommand(() => {
      throw new Error('clipboard blocked');
    });

    await expect(copyText('blocked payload')).resolves.toBe(false);
  });

  it('resolves false when the Clipboard API is absent and execCommand fails', async () => {
    vi.stubGlobal('navigator', {});
    setExecCommand(() => {
      throw new Error('clipboard blocked');
    });

    await expect(copyText('blocked payload')).resolves.toBe(false);
  });
});
