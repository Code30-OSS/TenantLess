import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen } from '@testing-library/react';

import type { JobSnapshot } from '../api/types';

/**
 * JobPanel (CTRL-02, D-15) — the live job state machine `queued → running → succeeded | failed`.
 *
 * `useJob` is mocked so each render state is asserted without a live poll. Polling itself STOPS on a
 * terminal status inside `useJob` (17-03, `jobRefetchInterval`) — not JobPanel's concern; here we pin
 * the RENDER contract per state, the accessible status region, and the bounded (non-live) log tail.
 *
 * Pins:
 *  - no id / no data → nothing rendered
 *  - queued/running/succeeded/failed each render the matching status pill
 *  - running renders the coarse phase label inside a role=status aria-live=polite region
 *  - the log tail is a bounded, NON-live `<pre aria-label="Job log">` (last LOG_TAIL_MAX lines)
 *  - failed renders the recovery-guidance banner
 */

const { useJobMock } = vi.hoisted(() => ({ useJobMock: vi.fn() }));
vi.mock('../api/control', () => ({ useJob: useJobMock }));

import JobPanel, { LOG_TAIL_MAX } from './JobPanel';

function snap(over: Partial<JobSnapshot>): JobSnapshot {
  return { status: 'queued', log: [], ...over };
}

function mockJob(data: JobSnapshot | undefined) {
  useJobMock.mockReturnValue({ data, isLoading: false, isError: false, error: null });
}

beforeEach(() => {
  useJobMock.mockReset();
});

describe('JobPanel — none state', () => {
  it('renders nothing when there is no id', () => {
    mockJob(undefined);
    const { container } = render(<JobPanel id={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when there is no job data yet', () => {
    mockJob(undefined);
    const { container } = render(<JobPanel id="job-1" />);
    expect(container.firstChild).toBeNull();
  });
});

describe('JobPanel — status pills across the state machine', () => {
  it.each(['queued', 'running', 'succeeded', 'failed'] as const)('renders the %s pill', (status) => {
    mockJob(snap({ status }));
    render(<JobPanel id="job-1" />);
    expect(screen.getByText(status)).toBeTruthy();
  });
});

describe('JobPanel — running: accessible status region + phase', () => {
  it('shows the phase in a role=status aria-live=polite region', () => {
    mockJob(snap({ status: 'running', phase: 'generating tenant…', log: ['line-1'] }));
    render(<JobPanel id="job-1" />);

    const region = screen.getByRole('status');
    expect(region.getAttribute('aria-live')).toBe('polite');
    expect(region.textContent).toContain('generating tenant…');
  });
});

describe('JobPanel — bounded, non-live log tail', () => {
  it('renders only the last LOG_TAIL_MAX lines in a non-live <pre aria-label="Job log">', () => {
    const lines = Array.from({ length: LOG_TAIL_MAX + 300 }, (_, i) => `log-line-${i}`);
    mockJob(snap({ status: 'running', log: lines }));
    render(<JobPanel id="job-1" />);

    const log = screen.getByLabelText('Job log');
    // NOT a live region (would flood a screen reader)
    expect(log.getAttribute('aria-live')).toBeNull();
    const rendered = (log.textContent ?? '').split('\n').filter((l) => l.length > 0);
    expect(rendered).toHaveLength(LOG_TAIL_MAX);
    // the tail: first kept line is line index 300, last is the final line
    expect(rendered[0]).toBe('log-line-300');
    expect(rendered[rendered.length - 1]).toBe(`log-line-${LOG_TAIL_MAX + 299}`);
  });
});

describe('JobPanel — succeeded: parsed result', () => {
  it('renders the parsed counts when result is present', () => {
    mockJob(
      snap({
        status: 'succeeded',
        log: ['done'],
        result: {
          tenant_id: 'abcd1234-ef56',
          subscriptions: 12,
          resource_groups: 40,
          resources: 5000,
          violations: 88,
        },
      }),
    );
    render(<JobPanel id="job-1" />);
    const text = screen.getByRole('status').textContent ?? '';
    expect(text).toContain('12 subscriptions');
    expect(text).toContain('5000 resources');
  });

  it('renders the unparsed fallback when exit-0 but no parsed result', () => {
    mockJob(snap({ status: 'succeeded', log: ['done'] }));
    render(<JobPanel id="job-1" />);
    expect(screen.getByText(/Completed — exit 0/i)).toBeTruthy();
  });
});

describe('JobPanel — elapsed anchors to job start, not component mount (WR-02)', () => {
  it('measures elapsed from when the job becomes active, not from panel mount', () => {
    vi.useFakeTimers();
    try {
      // Panel mounts while idle (no active job) — the operator sits on the screen.
      mockJob(undefined);
      const { rerender } = render(<JobPanel id={null} />);

      // Two minutes pass BEFORE any job starts.
      act(() => {
        vi.advanceTimersByTime(120_000);
      });

      // A job now starts (queued/running arrives with a fresh id).
      mockJob(snap({ status: 'running', log: [] }));
      rerender(<JobPanel id="job-1" />);

      // Three seconds of actual job runtime.
      act(() => {
        vi.advanceTimersByTime(3_000);
      });

      // Elapsed reflects the ~3s of job runtime, NOT the 123s since mount (the bug).
      expect(screen.getByText('3s')).toBeTruthy();
      expect(screen.queryByText('123s')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('JobPanel — failed: recovery guidance', () => {
  it('renders the recovery-guidance banner', () => {
    mockJob(snap({ status: 'failed', log: ['boom'] }));
    render(<JobPanel id="job-1" />);
    expect(screen.getByText(/reset to empty or regenerate to recover/i)).toBeTruthy();
  });
});
