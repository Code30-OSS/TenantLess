import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

/**
 * ScannerConfigView (S2 — DEMO-02, 18-UI-SPEC §S2) — the "point your scanner here" config view.
 *
 * Pins the S2 contract without a router or query client (the view is static, reading only the 18-02
 * config builders + the promoted `copyText` helper):
 *  - the three value labels (BASE URL / API-VERSION / AUTHORIZATION) + both snippet headings render;
 *  - the rendered DOM ships the GENERIC `SCANNER_ARM_ENDPOINT` / `SCANNER_STATIC_TOKEN` env names and
 *    contains NO forbidden vendor brand token — D-05 (locked, supersedes the UI-SPEC copy);
 *  - clicking a Copy button copies via the clipboard, flips its own label `Copy → Copied`, and the
 *    `aria-live` region announces it;
 *  - a fully-blocked clipboard (Clipboard API absent + `execCommand` throwing) never crashes the render
 *    and the button still flips (the `copyText` helper is best-effort, T-18-05).
 */

import ScannerConfigView from './ScannerConfigView';
import { forbiddenTokenPattern } from '../test-utils/scrubTokens';

const writeText = vi.fn();
const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');

function setClipboard(value: unknown) {
  Object.defineProperty(navigator, 'clipboard', { value, configurable: true, writable: true });
}

beforeEach(() => {
  writeText.mockReset();
  setClipboard({ writeText });
});

afterEach(() => {
  if (originalClipboard) {
    Object.defineProperty(navigator, 'clipboard', originalClipboard);
  } else {
    setClipboard(undefined);
  }
});

describe('ScannerConfigView — DEMO-02 scanner configuration', () => {
  it('renders the three value labels and both snippet headings', () => {
    render(<ScannerConfigView />);
    expect(screen.getByText('BASE URL')).toBeTruthy();
    expect(screen.getByText('API-VERSION')).toBeTruthy();
    expect(screen.getByText('AUTHORIZATION')).toBeTruthy();
    expect(screen.getByText('CURL')).toBeTruthy();
    expect(screen.getByText('STATIC-TOKEN SCANNER (PATH A)')).toBeTruthy();
  });

  it('ships the generic SCANNER_ env names and contains no forbidden brand token (D-05)', () => {
    const { container } = render(<ScannerConfigView />);
    expect(container.textContent).toContain('SCANNER_ARM_ENDPOINT');
    expect(container.textContent).toContain('SCANNER_STATIC_TOKEN');
    // Tokens come from tests/scrub-tokens.json plus the gitignored private
    // supplement -- never spelled in this source. They used to be assembled from
    // string fragments, which defeated the public/private split: deleting the
    // `+` signs reconstructed the private word list from a public file.
    const forbiddenBrand = forbiddenTokenPattern();
    expect(forbiddenBrand).not.toBeNull();
    expect(container.textContent ?? '').not.toMatch(forbiddenBrand!);
  });

  it('flips a Copy button to Copied and announces it via the aria-live region', async () => {
    render(<ScannerConfigView />);
    const copyButtons = screen.getAllByRole('button', { name: 'Copy' });
    expect(copyButtons.length).toBeGreaterThan(0);

    fireEvent.click(copyButtons[0]);

    expect(writeText).toHaveBeenCalledTimes(1);
    // The button's own label flips (exact match — the announcer text below differs so this is unique).
    expect(await screen.findByText('Copied')).toBeTruthy();
    // The polite live region announces the copy.
    const announcer = screen.getByText('Copied to clipboard');
    expect(announcer.getAttribute('aria-live')).toBe('polite');
  });

  it('shows "Copy failed" — never a false "Copied" — when the clipboard write rejects (UAT P2)', async () => {
    // Clipboard API present but writeText rejects (permission/policy) and there is no execCommand,
    // so both paths fail. The button must report failure honestly, not claim success.
    writeText.mockImplementation(() => Promise.reject(new Error('permission denied')));

    render(<ScannerConfigView />);
    const [firstCopy] = screen.getAllByRole('button', { name: 'Copy' });

    fireEvent.click(firstCopy);

    expect(await screen.findByText('Copy failed')).toBeTruthy();
    expect(screen.queryByText('Copied')).toBeNull();
  });

  it('does not crash and reports failure when copy is fully blocked (no Clipboard API + execCommand throws)', async () => {
    setClipboard(undefined);
    const execCommand = vi.fn(() => {
      throw new Error('copy blocked');
    });
    (document as unknown as { execCommand: unknown }).execCommand = execCommand;

    render(<ScannerConfigView />);
    const [firstCopy] = screen.getAllByRole('button', { name: 'Copy' });

    // Best-effort copy: a blocked clipboard must never throw into the render tree — and it must NOT
    // falsely report "Copied"; it flips to "Copy failed" instead.
    expect(() => fireEvent.click(firstCopy)).not.toThrow();
    expect(await screen.findByText('Copy failed')).toBeTruthy();
    expect(screen.queryByText('Copied')).toBeNull();

    delete (document as unknown as { execCommand?: unknown }).execCommand;
  });
});
