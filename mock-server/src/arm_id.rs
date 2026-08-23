//! Canonical ARM-ID identity (INV-01, D-01/D-02/D-25/D-28).
//!
//! The Rust half of the single documented normalization contract, byte-identical to
//! Python `tenantless.identity` and PostgreSQL `synthetic.ascii_fold` /
//! `synthetic.arm_id_key` — all three pinned by the shared KAT corpus
//! (`tests/kat/arm_id_kat.json`).
//!
//! Two semantic layers (D-28):
//! * [`ascii_fold`] — the low-level primitive: map ASCII `A-Z -> a-z` and NOTHING
//!   else (non-ASCII bytes, doubled / trailing slashes, and percent-encoded text
//!   all pass through unchanged). Used for identity COMPONENTS (RG names, resource
//!   names, provider / type segments).
//! * [`arm_id_key`] — `arm_id_key(id) == ascii_fold(id)` — the whole-ID wrapper,
//!   used whenever the value is a COMPLETE ARM id.
//!
//! [`ArmId`] owns both representations: `raw` (the client-verbatim wire casing,
//! served on every response, frozen at first write — D-25) and `key` (the single
//! identity for equality / collision / lookup / dedup / ownership — D-01).
//!
//! Implemented via `str::to_ascii_lowercase` (D-02) — explicitly NOT the locale
//! `to_lowercase()`, which diverges on Turkish dotted-I / sharp-s / across Unicode
//! versions. This is pure identity: NO structural parsing / validation (segment
//! parsing lives in `write_merge::parse_segments`).
//!
//! BOUNDARY (this unit is additive, behaviour-neutral, D-22a): this module is DEFINED
//! but consumed by NO handler yet. The five stateful seams migrate onto it in
//! a later cutover step. The in-memory [`audit_fold`] helper below is likewise AVAILABLE but not
//! wired into any boot path — the fail-loud pre-cutover migration audit that gates
//! the later cutover is the DB-backed Python helper (`writer.audit_arm_id_identity`).

/// Fold ASCII `A-Z` to `a-z`; leave every other byte unchanged (D-01/D-28).
///
/// The low-level identity primitive. `str::to_ascii_lowercase` remaps ONLY the 26
/// ASCII uppercase code points; every non-ASCII byte (including UTF-8 continuation
/// bytes, which are never in `0x41..=0x5A`) is preserved, so this agrees code-point
/// for code-point with the Python / PostgreSQL folds on the shared corpus.
pub fn ascii_fold(text: &str) -> String {
    text.to_ascii_lowercase()
}

/// Return the canonical identity `key` for a COMPLETE ARM id (D-01/D-28).
///
/// `arm_id_key(id) == ascii_fold(id)`.
pub fn arm_id_key(id: &str) -> String {
    ascii_fold(id)
}

/// The canonical ARM identity: a `raw` (client-verbatim, served — D-25) paired with
/// its derived `key` (identity — D-01). Pure identity; carries NO structural parse.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ArmId {
    raw: String,
    key: String,
}

impl ArmId {
    /// Construct from a wire id, freezing `raw` and deriving `key = ascii_fold(raw)`.
    pub fn new(raw: impl Into<String>) -> Self {
        let raw = raw.into();
        let key = ascii_fold(&raw);
        ArmId { raw, key }
    }

    /// The client-verbatim id, served on every response (D-25).
    pub fn raw(&self) -> &str {
        &self.raw
    }

    /// The normalized identity used for equality / collision / lookup (D-01).
    pub fn key(&self) -> &str {
        &self.key
    }
}

/// AVAILABLE (not-yet-wired) in-memory fold-collision audit — the std-only sibling
/// of the DB-backed D-04 migration audit. Given a set of raw ids, returns any group
/// of two-or-more DISTINCT raw ids that fold to the SAME `key` (a collision that the
/// identity contract would silently merge). An empty result means the set is
/// collision-free under the fold.
///
/// This is a pure helper (std-only, no DB): it mirrors the collision half of the
/// D-04 audit so a caller (e.g. a future conformance check) can assert non-merging
/// without a database round trip. It is NOT invoked by any boot / handler path in
/// this unit — the production fail-loud migration audit that gates the later
/// cutover is `writer.audit_arm_id_identity` against live PG.
pub fn audit_fold<I, S>(ids: I) -> Vec<(String, Vec<String>)>
where
    I: IntoIterator<Item = S>,
    S: Into<String>,
{
    use std::collections::BTreeMap;
    let mut buckets: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for id in ids {
        let raw: String = id.into();
        let key = arm_id_key(&raw);
        let bucket = buckets.entry(key).or_default();
        if !bucket.contains(&raw) {
            bucket.push(raw);
        }
    }
    buckets
        .into_iter()
        .filter(|(_, raws)| raws.len() > 1)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    // The SAME shared corpus the Python + live-PG KATs consume, so a drift in the
    // Rust fold fails here (D-05). Embedded at compile time from the repo-root
    // tests/ dir (this file lives at mock-server/src/, so ../../tests reaches it).
    const KAT_JSON: &str = include_str!("../../tests/kat/arm_id_kat.json");

    fn corpus() -> Vec<(String, String)> {
        let rows: Value = serde_json::from_str(KAT_JSON).expect("KAT corpus is valid JSON");
        let arr = rows.as_array().expect("KAT corpus is a JSON array");
        assert!(!arr.is_empty(), "KAT corpus must be non-empty");
        arr.iter()
            .map(|r| {
                (
                    r["input"].as_str().expect("input is a string").to_string(),
                    r["key"].as_str().expect("key is a string").to_string(),
                )
            })
            .collect()
    }

    #[test]
    fn arm_id_key_matches_every_corpus_row() {
        for (input, key) in corpus() {
            assert_eq!(arm_id_key(&input), key, "arm_id_key mismatch on {input:?}");
        }
    }

    #[test]
    fn armid_key_matches_corpus_and_preserves_raw() {
        for (input, key) in corpus() {
            let id = ArmId::new(&input);
            assert_eq!(id.key(), key, "ArmId::key mismatch on {input:?}");
            // raw is frozen client-verbatim (D-25) — never folded.
            assert_eq!(id.raw(), input, "ArmId::raw must preserve wire casing");
        }
    }

    #[test]
    fn arm_id_key_is_ascii_fold() {
        for (input, _) in corpus() {
            assert_eq!(arm_id_key(&input), ascii_fold(&input));
        }
    }

    #[test]
    fn fold_diverges_from_locale_lowercase() {
        // The load-bearing divergence: locale to_lowercase() expands the Turkish
        // dotted-I (İ -> "i̇") while the ASCII fold leaves it untouched. This is
        // exactly the cross-engine drift the contract forbids (D-02).
        let dotted = "/İSTANBUL";
        assert_eq!(ascii_fold(dotted), "/İstanbul");
        assert_ne!(ascii_fold(dotted), dotted.to_lowercase());
        // Sharp-s is preserved (never casefold-expanded to "ss").
        assert_eq!(ascii_fold("/Straße"), "/straße");
    }

    #[test]
    fn audit_fold_flags_case_only_collisions() {
        // Two distinct raws that fold to one key are reported.
        let collisions = audit_fold(["/Sub/RG", "/sub/rg", "/other"]);
        assert_eq!(collisions.len(), 1);
        assert_eq!(collisions[0].0, "/sub/rg");
        assert_eq!(collisions[0].1.len(), 2);
        // A collision-free set reports nothing.
        assert!(audit_fold(["/a", "/b", "/c"]).is_empty());
    }
}
