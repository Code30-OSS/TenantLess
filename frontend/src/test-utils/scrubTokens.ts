/**
 * Shared scrub-token loader for frontend tests (D-4).
 *
 * Test-only: nothing in the application imports this, so it never reaches the
 * production bundle.
 *
 * WHY THIS EXISTS
 * ---------------
 * Several demo tests used to assemble the forbidden tokens inline, splitting
 * each one across two adjacent string literals joined with `+`, so the file
 * asserting "this artifact contains no forbidden token" would not itself trip
 * the whole-tree scrub gate. That defeated the entire public/private split: a
 * reader of the public repository could reconstruct the private word list by
 * deleting the `+` signs. The Stage 3 human review rejected the export over
 * exactly this.
 *
 * The tokens now live in data:
 *   - tests/scrub-tokens.json          committed, public, generic sentinels
 *   - tests/.scrub-tokens.private.json gitignored, the real internal names
 *
 * A public checkout gets a real, non-vacuous gate over the generic set; a
 * maintainer's checkout additionally covers the private list. No source file
 * has to obfuscate anything.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
// frontend/src/test-utils -> repo root
const TESTS_DIR = resolve(HERE, '..', '..', '..', 'tests');

function readTokens(file: string): string[] {
  try {
    const raw = readFileSync(resolve(TESTS_DIR, file), 'utf-8');
    const parsed = JSON.parse(raw) as { tokens?: unknown };
    if (!Array.isArray(parsed.tokens)) return [];
    return parsed.tokens
      .filter((t): t is string => typeof t === 'string')
      .map((t) => t.trim().toLowerCase())
      .filter(Boolean);
  } catch {
    // Absent private supplement is the normal public case.
    return [];
  }
}

/** Public tokens plus the private supplement when this checkout has one. */
export function allScrubTokens(): string[] {
  const merged = [...readTokens('scrub-tokens.json'), ...readTokens('.scrub-tokens.private.json')];
  return [...new Set(merged)];
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Whole-identifier, case-insensitive matcher over the configured tokens.
 *
 * Returns `null` for an empty token set. Callers must treat that as "cannot
 * check", never as "clean" — a matcher built from zero tokens would pass
 * everything.
 */
export function forbiddenTokenPattern(): RegExp | null {
  const tokens = allScrubTokens();
  if (tokens.length === 0) return null;
  return new RegExp(`(?<![a-z])(${tokens.map(escapeRegExp).join('|')})(?![a-z])`, 'i');
}
