import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import type { Snapshot } from '../api/types';
import SnapshotList from './SnapshotList';

/**
 * SnapshotList (CTRL-04) — WR-01 regression: the created-at column must render from the SERVER's
 * actual wire shape `{ name, createdUnix }` (snapshot.rs `SnapshotEntry`, `created_unix` renamed
 * `createdUnix`), NOT the stale `{ createdAt, size }` the frontend used to declare. On the old
 * shape `rowMeta` read `s.createdAt` (always undefined) → the meta line rendered blank.
 */

function noop() {}

function baseProps(snapshots: Snapshot[]) {
  return {
    snapshots,
    loading: false,
    busy: false,
    selectedName: null,
    onRestore: noop,
    onDelete: noop,
    onSaveFirst: noop,
  };
}

describe('SnapshotList — created-at renders from createdUnix (WR-01)', () => {
  it('formats createdUnix (unix seconds) into a readable created-at, not blank/Invalid Date', () => {
    const createdUnix = 1_752_019_200; // a fixed unix-seconds instant
    render(<SnapshotList {...baseProps([{ name: 's1', createdUnix }])} />);

    const expected = new Date(createdUnix * 1000).toLocaleString();
    // The meta line for the row shows the formatted created-at (exact — no size is ever sent).
    expect(screen.getByText(expected)).toBeTruthy();
    // Never a blank meta line or an Invalid Date artifact.
    expect(screen.queryByText('Invalid Date')).toBeNull();
  });
});
