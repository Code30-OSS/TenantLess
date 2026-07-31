import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';

import ErrorBoundary from './ErrorBoundary';

/**
 * App-level ErrorBoundary (defense-in-depth for the empty-tenant / any-view crash).
 *
 * A single uncaught render throw in a routed view used to unmount the whole AppShell
 * tree → a fully blank page. The boundary must catch a child throw and degrade to an
 * on-brand in-shell fallback (never rethrow, never blank), while passing non-throwing
 * children straight through.
 */

/** A child that throws on its first render. */
function Boom(): never {
  throw new Error('boom');
}

describe('ErrorBoundary', () => {
  it('renders its children unchanged when they do not throw', () => {
    render(
      <ErrorBoundary>
        <div>safe child</div>
      </ErrorBoundary>,
    );
    expect(screen.getByText('safe child')).toBeTruthy();
    expect(screen.queryByText(/something went wrong/i)).toBeNull();
  });

  describe('when a child throws', () => {
    let errorSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
      // React logs the caught error to console.error — silence the expected noise.
      errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    });

    afterEach(() => {
      errorSpy.mockRestore();
    });

    it('renders the on-brand fallback without rethrowing', () => {
      expect(() =>
        render(
          <ErrorBoundary>
            <Boom />
          </ErrorBoundary>,
        ),
      ).not.toThrow();

      expect(screen.getByText(/something went wrong/i)).toBeTruthy();
      expect(screen.queryByText('safe child')).toBeNull();
    });
  });
});
