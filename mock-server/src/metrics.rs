//! In-memory request-activity metrics + live event broadcast for the `/_console`
//! dashboard.
//!
//! This is a local dev-observability surface only — it never touches the DB and
//! is deliberately bounded: a fixed-capacity ring of the most recent events, small
//! aggregate counters, and a rolling time-bucket ring that yields a
//! p50/p95/max-over-time latency series (the substrate behind `GET /_console/history`),
//! all behind a [`Metrics`] handle that is cloned into [`crate::state::AppState`].
//! The recording middleware ([`record_metrics`]) pushes one [`RequestEvent`] per
//! served ARM request; the dashboard SSE endpoint subscribes to the same broadcast
//! channel for an instant live feed.
//!
//! The [`BucketRing`] rotates lazily off each event's `ts_ms` (write) and the real
//! clock (read) with no background timer, and stays bounded in memory by a fixed
//! bucket count plus a per-bucket latency-sample cap while keeping an exact count.
//!
//! Nothing here is on the ARM hot path's critical contract: every lock is held
//! synchronously (never across an `.await`), and a poisoned lock or a full
//! broadcast channel degrades the dashboard, never the API response.

use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use axum::{
    extract::{MatchedPath, Request, State},
    middleware::Next,
    response::Response,
};
use serde::Serialize;
use tokio::sync::broadcast;

use crate::state::AppState;

/// How many recent events the ring retains (drives the initial table + percentiles).
const RING_CAPACITY: usize = 2000;
/// Broadcast backlog before slow SSE subscribers start lagging (and skip events).
const BROADCAST_CAPACITY: usize = 256;
/// Width of one history bucket in milliseconds (~1s granularity, D-04).
const BUCKET_MS: u64 = 1_000;
/// How many buckets the rolling history window retains (~5-min window at 1s, D-04).
const WINDOW_BUCKETS: usize = 300;
/// Per-bucket retained latency-sample cap; the request `count` stays exact even
/// when the sample is capped under a burst (memory guard — see the module-level
/// "degrade the dashboard, never the API" invariant).
const MAX_SAMPLES_PER_BUCKET: usize = 8_192;

/// One served request, as shown in the live feed. `route` is the matched axum
/// path template (e.g. `/subscriptions/{sub}/resources`), not the concrete URL,
/// so the per-route aggregate stays bounded.
#[derive(Clone, Debug, Serialize)]
pub struct RequestEvent {
    /// Unix epoch milliseconds when the response completed.
    pub ts_ms: u64,
    pub method: String,
    /// Concrete request path (with the real subscription / RG ids).
    pub path: String,
    /// Matched route template, used for the per-route breakdown.
    pub route: String,
    pub status: u16,
    pub latency_ms: u64,
}

/// Cloneable handle over the shared activity state. Clones share one inner via `Arc`.
#[derive(Clone)]
pub struct Metrics {
    inner: Arc<Inner>,
}

struct Inner {
    ring: Mutex<VecDeque<RequestEvent>>,
    counters: Mutex<Counters>,
    buckets: Mutex<BucketRing>,
    tx: broadcast::Sender<RequestEvent>,
}

#[derive(Default)]
struct Counters {
    total: u64,
    by_status: BTreeMap<u16, u64>,
    by_route: BTreeMap<String, u64>,
}

impl Default for Metrics {
    fn default() -> Self {
        Self::new()
    }
}

impl Metrics {
    pub fn new() -> Self {
        let (tx, _rx) = broadcast::channel(BROADCAST_CAPACITY);
        Metrics {
            inner: Arc::new(Inner {
                ring: Mutex::new(VecDeque::with_capacity(RING_CAPACITY)),
                counters: Mutex::new(Counters::default()),
                buckets: Mutex::new(BucketRing::new()),
                tx,
            }),
        }
    }

    /// Record one served request: append to the ring (evicting the oldest past
    /// capacity), bump the aggregate counters, and broadcast to live subscribers.
    /// A send error just means no dashboard is open — that is expected, not a fault.
    pub fn record(&self, event: RequestEvent) {
        if let Ok(mut ring) = self.inner.ring.lock() {
            if ring.len() == RING_CAPACITY {
                ring.pop_front();
            }
            ring.push_back(event.clone());
        }
        if let Ok(mut c) = self.inner.counters.lock() {
            c.total += 1;
            *c.by_status.entry(event.status).or_insert(0) += 1;
            *c.by_route.entry(event.route.clone()).or_insert(0) += 1;
        }
        if let Ok(mut b) = self.inner.buckets.lock() {
            b.insert(event.ts_ms, event.latency_ms as u32);
        }
        let _ = self.inner.tx.send(event);
    }

    /// A fresh subscriber to the live event stream (used by the SSE endpoint).
    pub fn subscribe(&self) -> broadcast::Receiver<RequestEvent> {
        self.inner.tx.subscribe()
    }

    /// A point-in-time snapshot for the dashboard's initial load and periodic
    /// aggregate refresh. Percentiles are computed over the current ring.
    pub fn snapshot(&self) -> StatsSnapshot {
        let (total, by_status, by_route) = match self.inner.counters.lock() {
            Ok(c) => (c.total, c.by_status.clone(), c.by_route.clone()),
            Err(_) => (0, BTreeMap::new(), BTreeMap::new()),
        };

        let recent: Vec<RequestEvent> = match self.inner.ring.lock() {
            // Newest first for direct rendering in the table.
            Ok(ring) => ring.iter().rev().cloned().collect(),
            Err(_) => Vec::new(),
        };

        let mut latencies: Vec<u64> = recent.iter().map(|e| e.latency_ms).collect();
        latencies.sort_unstable();
        let p50_ms = percentile(&latencies, 50.0);
        let p95_ms = percentile(&latencies, 95.0);
        let max_ms = latencies.last().copied().unwrap_or(0);

        StatsSnapshot {
            total,
            by_status: by_status
                .into_iter()
                .map(|(k, v)| (k.to_string(), v))
                .collect(),
            by_route,
            p50_ms,
            p95_ms,
            max_ms,
            recent,
        }
    }

    /// The rolling p50/p95/max-over-time series for `GET /_console/history`.
    /// Rotates the ring to the real clock first, so idle seconds since the last
    /// request appear as trailing empty buckets. Reads via the pure
    /// [`BucketRing::history_at`]; a poisoned lock degrades to an empty series.
    pub fn history(&self) -> HistorySnapshot {
        let server_now_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0);
        let buckets = match self.inner.buckets.lock() {
            Ok(b) => b.history_at(server_now_ms),
            Err(_) => Vec::new(),
        };
        HistorySnapshot {
            bucket_ms: BUCKET_MS,
            window_ms: BUCKET_MS * WINDOW_BUCKETS as u64,
            server_now_ms,
            buckets,
        }
    }
}

/// Serializable aggregate snapshot returned by `GET /_console/stats`.
#[derive(Serialize)]
pub struct StatsSnapshot {
    pub total: u64,
    /// Status code (as string, for JSON object keys) → count.
    pub by_status: BTreeMap<String, u64>,
    /// Matched route template → count.
    pub by_route: BTreeMap<String, u64>,
    pub p50_ms: u64,
    pub p95_ms: u64,
    pub max_ms: u64,
    /// Most-recent-first slice of the ring (drives the initial table render).
    pub recent: Vec<RequestEvent>,
}

/// One rolling-history time bucket. `epoch` records which `BUCKET_MS`-slice this
/// slot currently holds (a slot is reused across the ring); `count` is the exact
/// request count for that slice, and `latencies` is a bounded latency sample.
struct Bucket {
    epoch: u64,
    count: u64,
    latencies: Vec<u32>,
}

/// A fixed-capacity ring of `WINDOW_BUCKETS` time buckets, indexed by
/// `epoch % WINDOW_BUCKETS`, rotated lazily off request timestamps (no timer).
struct BucketRing {
    slots: Vec<Bucket>,
    head_epoch: u64,
}

impl BucketRing {
    fn new() -> Self {
        let slots = (0..WINDOW_BUCKETS)
            .map(|_| Bucket {
                epoch: 0,
                count: 0,
                latencies: Vec::new(),
            })
            .collect();
        BucketRing {
            slots,
            head_epoch: 0,
        }
    }

    /// Record one request's latency into the bucket for `ts_ms`, rotating the ring
    /// forward lazily (no timer). Newly entered slots are reset on entry; a gap
    /// wider than the window resets every slot. An out-of-window *older* event is
    /// dropped rather than clobbering a newer bucket. The exact `count` is always
    /// bumped; the latency sample is retained only under `MAX_SAMPLES_PER_BUCKET`.
    fn insert(&mut self, ts_ms: u64, latency_ms: u32) {
        let e = ts_ms / BUCKET_MS;

        if e > self.head_epoch {
            let gap = e - self.head_epoch;
            if gap >= WINDOW_BUCKETS as u64 {
                // Every retained bucket is now stale — reset the whole ring.
                for slot in &mut self.slots {
                    slot.epoch = 0;
                    slot.count = 0;
                    slot.latencies.clear();
                }
            } else {
                // Reset each slot we advance into (epochs head+1..=e).
                for ep in (self.head_epoch + 1)..=e {
                    let slot = &mut self.slots[(ep % WINDOW_BUCKETS as u64) as usize];
                    slot.epoch = ep;
                    slot.count = 0;
                    slot.latencies.clear();
                }
            }
            self.head_epoch = e;
        }

        let slot = &mut self.slots[(e % WINDOW_BUCKETS as u64) as usize];
        if slot.epoch != e {
            if e < self.head_epoch {
                // Too old — its bucket has already rotated out of the window.
                return;
            }
            // Claim the slot for this epoch (reset-all path leaves epoch == 0).
            slot.epoch = e;
            slot.count = 0;
            slot.latencies.clear();
        }
        slot.count += 1;
        if slot.latencies.len() < MAX_SAMPLES_PER_BUCKET {
            slot.latencies.push(latency_ms);
        }
    }

    /// Emit the contiguous oldest→newest series of exactly `WINDOW_BUCKETS` buckets
    /// as of `now_ms`. Buckets with no matching epoch (idle gaps, including the
    /// trailing seconds since the last request) carry `count: 0` and `None`
    /// percentiles — never `Some(0)` — so the chart breaks the line instead of
    /// drawing it to zero.
    fn history_at(&self, now_ms: u64) -> Vec<BucketDto> {
        let newest = (now_ms / BUCKET_MS) as i64;
        let oldest = newest - (WINDOW_BUCKETS as i64 - 1);
        let mut out = Vec::with_capacity(WINDOW_BUCKETS);
        for ep in oldest..=newest {
            if ep < 0 {
                // Pre-epoch-zero slots never held data (only when now_ms is small,
                // e.g. in unit tests) — emit an empty placeholder.
                out.push(empty_bucket(0));
                continue;
            }
            let e = ep as u64;
            let slot = &self.slots[(e % WINDOW_BUCKETS as u64) as usize];
            if slot.epoch == e && slot.count > 0 {
                let mut sorted: Vec<u64> = slot.latencies.iter().map(|&l| l as u64).collect();
                sorted.sort_unstable();
                out.push(BucketDto {
                    ts_ms: e * BUCKET_MS,
                    count: slot.count,
                    p50_ms: Some(percentile(&sorted, 50.0)),
                    p95_ms: Some(percentile(&sorted, 95.0)),
                    max_ms: sorted.last().copied(),
                });
            } else {
                out.push(empty_bucket(e * BUCKET_MS));
            }
        }
        out
    }
}

/// An empty (no-traffic) history bucket at `ts_ms`: exact count 0 and null
/// percentiles (absence is not zero latency).
fn empty_bucket(ts_ms: u64) -> BucketDto {
    BucketDto {
        ts_ms,
        count: 0,
        p50_ms: None,
        p95_ms: None,
        max_ms: None,
    }
}

/// One serialized history bucket. Empty buckets carry `count: 0` and `None`
/// percentiles (never `Some(0)`) so an idle gap breaks the chart line, not zeroes it.
#[derive(Serialize)]
pub struct BucketDto {
    pub ts_ms: u64,
    pub count: u64,
    pub p50_ms: Option<u64>,
    pub p95_ms: Option<u64>,
    pub max_ms: Option<u64>,
}

/// Serializable rolling-history series returned by `GET /_console/history`.
#[derive(Serialize)]
pub struct HistorySnapshot {
    pub bucket_ms: u64,
    pub window_ms: u64,
    pub server_now_ms: u64,
    pub buckets: Vec<BucketDto>,
}

/// Nearest-rank percentile over a pre-sorted slice. Empty → 0.
fn percentile(sorted: &[u64], p: f64) -> u64 {
    if sorted.is_empty() {
        return 0;
    }
    // Nearest-rank: ceil(p/100 * N), clamped to a valid 0-based index.
    let rank = (p / 100.0 * sorted.len() as f64).ceil() as usize;
    let idx = rank.saturating_sub(1).min(sorted.len() - 1);
    sorted[idx]
}

/// Axum middleware that times each request and records a [`RequestEvent`].
///
/// Applied to the ARM router only (the dashboard routes are intentionally NOT
/// instrumented, so the SSE/stats polling never floods the feed). It is the
/// outermost layer, so the recorded `status` reflects the final response —
/// including a 401 from the bearer layer it wraps.
pub async fn record_metrics(
    State(state): State<AppState>,
    request: Request,
    next: Next,
) -> Response {
    let method = request.method().to_string();
    let path = request.uri().path().to_string();
    let route = request
        .extensions()
        .get::<MatchedPath>()
        .map(|m| m.as_str().to_string())
        .unwrap_or_else(|| path.clone());

    let start = Instant::now();
    let response = next.run(request).await;
    let latency_ms = start.elapsed().as_millis() as u64;

    let ts_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);

    state.metrics.record(RequestEvent {
        ts_ms,
        method,
        path,
        route,
        status: response.status().as_u16(),
        latency_ms,
    });

    response
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ev(ts_ms: u64, status: u16, latency_ms: u64, route: &str) -> RequestEvent {
        RequestEvent {
            ts_ms,
            method: "GET".to_string(),
            path: route.to_string(),
            route: route.to_string(),
            status,
            latency_ms,
        }
    }

    #[test]
    fn percentile_nearest_rank() {
        let sorted = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
        assert_eq!(percentile(&sorted, 50.0), 5);
        assert_eq!(percentile(&sorted, 95.0), 10);
        assert_eq!(percentile(&sorted, 100.0), 10);
        assert_eq!(percentile(&[], 50.0), 0);
        assert_eq!(percentile(&[42], 95.0), 42);
    }

    #[test]
    fn record_updates_counters_and_snapshot() {
        let m = Metrics::new();
        m.record(ev(0, 200, 10, "/subscriptions"));
        m.record(ev(0, 200, 30, "/subscriptions"));
        m.record(ev(0, 401, 1, "/subscriptions/{sub}/resources"));

        let s = m.snapshot();
        assert_eq!(s.total, 3);
        assert_eq!(s.by_status.get("200"), Some(&2));
        assert_eq!(s.by_status.get("401"), Some(&1));
        assert_eq!(s.by_route.get("/subscriptions"), Some(&2));
        assert_eq!(s.recent.len(), 3);
        // Newest first.
        assert_eq!(s.recent[0].status, 401);
        assert_eq!(s.max_ms, 30);
    }

    #[test]
    fn ring_is_bounded_to_capacity() {
        let m = Metrics::new();
        for i in 0..(RING_CAPACITY + 50) {
            m.record(ev(0, 200, i as u64, "/subscriptions"));
        }
        let s = m.snapshot();
        assert_eq!(s.recent.len(), RING_CAPACITY, "ring must cap at capacity");
        assert_eq!(
            s.total,
            (RING_CAPACITY + 50) as u64,
            "total counts all events"
        );
    }

    #[test]
    fn subscribe_receives_broadcast_events() {
        let m = Metrics::new();
        let mut rx = m.subscribe();
        m.record(ev(0, 200, 5, "/subscriptions"));
        let got = rx.try_recv().expect("event delivered to subscriber");
        assert_eq!(got.status, 200);
        assert_eq!(got.latency_ms, 5);
    }

    /// Rotation keys off `ts_ms`: two events in one BUCKET_MS epoch land in the
    /// same slot (count 2); a third in a later epoch lands in its own slot.
    #[test]
    fn bucket_rotates_off_ts_ms() {
        let mut ring = BucketRing::new();
        let e = 100u64;
        ring.insert(e * BUCKET_MS, 5);
        ring.insert(e * BUCKET_MS + 10, 7); // same epoch E
        ring.insert((e + 2) * BUCKET_MS, 9); // epoch E+2

        let slot_e = &ring.slots[(e % WINDOW_BUCKETS as u64) as usize];
        assert_eq!(slot_e.epoch, e, "slot holds epoch E");
        assert_eq!(slot_e.count, 2, "two events in epoch E");

        let slot_e2 = &ring.slots[((e + 2) % WINDOW_BUCKETS as u64) as usize];
        assert_eq!(slot_e2.epoch, e + 2, "slot holds epoch E+2");
        assert_eq!(slot_e2.count, 1, "one event in epoch E+2");
    }

    /// An idle gap between the last event and `now_ms` surfaces as trailing empty
    /// buckets: count 0 and `None` percentiles (never `Some(0)`, Pitfall 5).
    #[test]
    fn bucket_idle_gap_fills_empty() {
        let mut ring = BucketRing::new();
        let e = 100u64;
        ring.insert(e * BUCKET_MS, 5);

        let buckets = ring.history_at((e + 5) * BUCKET_MS);
        assert_eq!(buckets.len(), WINDOW_BUCKETS, "full window emitted");

        // Newest bucket (epoch e+5) is idle → empty with null percentiles.
        let last = buckets.last().unwrap();
        assert_eq!(last.count, 0);
        assert_eq!(last.p50_ms, None);
        assert_eq!(last.p95_ms, None);
        assert_eq!(last.max_ms, None);

        // Exactly one populated bucket (epoch e, count 1); everything after it empty.
        let populated: Vec<&BucketDto> = buckets.iter().filter(|b| b.count > 0).collect();
        assert_eq!(populated.len(), 1);
        assert_eq!(populated[0].count, 1);

        let e_idx = buckets.iter().position(|b| b.count > 0).unwrap();
        for b in &buckets[e_idx + 1..] {
            assert_eq!(b.count, 0);
            assert_eq!(b.p50_ms, None, "empty bucket has null p50, not Some(0)");
        }
    }

    /// Per-bucket percentiles equal the shared `percentile()` helper over the
    /// known input; max is the largest sample.
    #[test]
    fn bucket_percentiles_match_known_input() {
        let mut ring = BucketRing::new();
        let e = 50u64;
        let lats: [u64; 10] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
        for &l in &lats {
            ring.insert(e * BUCKET_MS, l as u32);
        }

        let buckets = ring.history_at(e * BUCKET_MS);
        let last = buckets.last().unwrap();
        assert_eq!(last.count, 10);

        let mut sorted: Vec<u64> = lats.to_vec();
        sorted.sort_unstable();
        assert_eq!(last.p50_ms, Some(percentile(&sorted, 50.0)));
        assert_eq!(last.p95_ms, Some(percentile(&sorted, 95.0)));
        assert_eq!(last.max_ms, Some(*sorted.last().unwrap()));
    }

    /// The retained latency sample is capped at MAX_SAMPLES_PER_BUCKET while the
    /// request `count` stays exact (degrade the dashboard, never the API).
    #[test]
    fn bucket_sample_capped_count_exact() {
        let mut ring = BucketRing::new();
        let e = 7u64;
        let n = MAX_SAMPLES_PER_BUCKET + 100;
        for i in 0..n {
            ring.insert(e * BUCKET_MS, (i % 50) as u32);
        }

        let slot = &ring.slots[(e % WINDOW_BUCKETS as u64) as usize];
        assert_eq!(
            slot.latencies.len(),
            MAX_SAMPLES_PER_BUCKET,
            "latency sample capped"
        );
        assert_eq!(slot.count, n as u64, "exact count preserved beyond the cap");
    }

    /// The public `history()` reads the real clock and always yields a full
    /// oldest→newest window; monotonic non-decreasing `ts_ms`; and — with no
    /// traffic — every bucket empty with null percentiles.
    #[test]
    fn history_returns_full_window_oldest_to_newest() {
        let m = Metrics::new();
        let snap = m.history();

        assert_eq!(snap.bucket_ms, BUCKET_MS);
        assert_eq!(snap.window_ms, BUCKET_MS * WINDOW_BUCKETS as u64);
        assert_eq!(snap.buckets.len(), WINDOW_BUCKETS, "full window emitted");

        // Oldest → newest ordering.
        for pair in snap.buckets.windows(2) {
            assert!(pair[0].ts_ms <= pair[1].ts_ms, "buckets are oldest→newest");
        }
        // No traffic recorded → every bucket empty with null percentiles.
        for b in &snap.buckets {
            assert_eq!(b.count, 0);
            assert_eq!(b.p50_ms, None);
            assert_eq!(b.p95_ms, None);
            assert_eq!(b.max_ms, None);
        }
    }
}
