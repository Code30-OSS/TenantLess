/**
 * ErrorBoundary — app-level React error boundary (defense-in-depth, WEBUI-04).
 *
 * The AppShell mounts the Topbar and the routed `<Outlet/>` as siblings with no boundary,
 * so a single uncaught render throw in ANY view used to unmount the whole tree → a blank
 * page (chrome and all). This class component catches a child render throw, logs it, and
 * renders a minimal on-brand fallback INSIDE the shell instead of blanking the app. The
 * Topbar's own crash-safety comes from its null-guard (it is a sibling of Outlet, outside
 * this boundary); this boundary isolates the VIEW.
 *
 * Only a generic "Something went wrong" + a Refresh affordance is surfaced to the DOM — no
 * error message or stack reaches the page; the detail goes to console.error only.
 */
import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Log for diagnostics; do NOT rethrow (rethrowing would re-blank the tree).
    console.error('ErrorBoundary caught a view render error:', error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        <div role="alert" style={{ padding: '2rem', textAlign: 'center' }}>
          <h2>Something went wrong</h2>
          <p>The current view failed to render. The rest of the console is unaffected.</p>
          <button type="button" onClick={() => window.location.reload()}>
            Refresh
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
