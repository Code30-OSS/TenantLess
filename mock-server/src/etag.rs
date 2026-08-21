//! ETag *derivation* + conditional-precondition evaluation for the stateful ARM write plane.
//!
//! The derivation helpers ([`overlay_etag`] / [`baseline_resource_etag`] / [`baseline_rg_etag`])
//! are pure and standalone (unit/KAT-tested here). As of Phase 23 they ARE wired: the read
//! handlers emit the `ETag` header from these tokens, and the write handlers consume
//! `If-Match` / `If-None-Match` through [`evaluate_precondition`] below (D-06/D-07/D-15/D-22).
//!
//! # Two ETag domains
//! * Overlay / resurrected rows → `"o-<decimal-revision>"` — canonical decimal, no
//!   leading zeros, prefix INSIDE the quotes. No hashing: a mutation allocates a new
//!   revision → a new ETag, so it is lost-update-safe by construction.
//! * Baseline resource / baseline RG → `"b-<lowercase-64-char-sha256-hex>"` over a
//!   VERSIONED canonical preimage of the *served* representation.
//!
//! Both tokens are STRONG (never `W/`), quoted, byte-for-byte comparable,
//! and the `b-`/`o-` prefix lives inside the quotes so the two domains can never collide.
//!
//! # Canonical `b-` preimage — documented byte layout (the KAT is a SPEC, not a snapshot)
//!
//! Every variable field is length-prefixed with a fixed **u64 little-endian** byte
//! count, so concatenation is unambiguous (no `("a","bc")` vs `("ab","c")` collision).
//! JSON `Value` fields are canonicalized by `serde_json::to_writer` over the
//! `BTreeMap`-backed `Value::Object` (sorted keys, recursively — `preserve_order` is OFF,
//! canary-guarded). The exact frame, in order, for a **resource** (marker
//! `tenantless-etag-v1:resource`):
//!
//! ```text
//!   LP(marker)               u64_le(len) ++ marker_bytes          (marker itself length-prefixed)
//!   LP(id)                   u64_le(len) ++ id_utf8
//!   LP(name)
//!   LP(type)                 (already canonical-cased by From<ResourceRow>)
//!   LP(location)
//!   LP_JSON(tags)            u64_le(json_len) ++ serde_json(tags)
//!   OPT_JSON(sku)            0x00 if None  |  0x01 ++ LP_JSON(sku) if Some
//!   OPT_STR(kind)            0x00 if None  |  0x01 ++ LP(kind)     if Some
//!   LP_JSON(properties)      never JSON null here — From<ResourceRow> coalesces null→{}
//! ```
//!
//! For a **resource group** (marker `tenantless-etag-v1:resource-group`):
//! `LP(marker) LP(id) LP(name) LP(type) LP(location) LP_JSON(tags) LP_JSON(properties)`,
//! where `type` is the served CONSTANT `Microsoft.Resources/resourceGroups` and
//! `properties` is the synthesized `{"provisioningState": …}` object (an RG's
//! provisioning state IS served, inside `properties`).
//!
//! `LP_JSON` needs the serialized byte length up front but MUST NOT build a second
//! serialized buffer. It measures the length with a first `to_writer` pass
//! into a byte-counting sink (which allocates nothing and discards its bytes), then a
//! second `to_writer` pass streams the identical bytes straight into the ONE reused
//! `Sha256`. Only one hasher is ever constructed per derivation.
//!
//! The frozen KAT digests in the tests were computed INDEPENDENTLY (Python `hashlib`
//! over the byte layout above), not captured from a first green run.

use serde_json::Value;
use sha2::{Digest, Sha256};
use std::io::{self, Write};

/// Derive the overlay/resurrected ETag: `"o-<decimal-revision>"`. Canonical
/// decimal, no leading zeros, prefix inside the quotes, no hashing. The revision is the
/// `BIGINT` `arm_overlay.revision` value (Postgres `bigint` → Rust `i64`).
pub fn overlay_etag(revision: i64) -> String {
    format!("\"o-{revision}\"")
}

/// Derive the baseline-resource ETag: `"b-<lowercase-64-hex>"` over the versioned
/// canonical preimage of the served `Resource` representation.
///
/// The `let Resource { .. }` is EXHAUSTIVE (no `..`): a newly-served DTO field becomes a
/// COMPILE error here until the preimage/version marker is consciously updated.
pub fn baseline_resource_etag(r: &crate::arm::Resource) -> String {
    // Exhaustive destructure — DTO-coupling guard. Adding a served field to
    // `crate::arm::Resource` will fail to compile until it is framed below.
    let crate::arm::Resource {
        id,
        name,
        r#type,
        location,
        tags,
        sku,
        kind,
        properties,
    } = r;

    let mut h = Sha256::new();
    // Hardening debt: the exhaustive destructure above forces a contributor to
    // ACKNOWLEDGE a new served field, but does NOT force bumping the `-v1` marker — a field
    // could be folded into the existing hash under the same version (the frozen KAT would
    // change, yet could be updated in the same commit). Tracked as a follow-up.
    write_marker(&mut h, b"tenantless-etag-v1:resource");
    write_lp_str(&mut h, id);
    write_lp_str(&mut h, name);
    write_lp_str(&mut h, r#type);
    write_lp_str(&mut h, location);
    write_lp_json(&mut h, tags);
    write_opt_json(&mut h, sku);
    write_opt_str(&mut h, kind);
    write_lp_json(&mut h, properties);
    finish(h)
}

/// Derive the baseline resource-group ETag: `"b-<lowercase-64-hex>"` over the versioned
/// canonical preimage of the served `ResourceGroup` representation.
///
/// The `let ResourceGroup { .. }` is EXHAUSTIVE (no `..`): a newly-served RG field becomes
/// a COMPILE error until the preimage/version marker is consciously updated. The
/// served `type` CONSTANT (`Microsoft.Resources/resourceGroups`) participates in the
/// preimage, and together with the distinct `:resource-group` marker guarantees an RG token
/// can never collide with a resource token of the same id. `properties` carries the
/// synthesized `{"provisioningState": …}` (an RG's provisioning state IS served). Reuses the
/// framing helpers and the single reused-`Sha256` streaming path.
pub fn baseline_rg_etag(rg: &crate::arm::ResourceGroup) -> String {
    // Exhaustive destructure — DTO-coupling guard.
    let crate::arm::ResourceGroup {
        id,
        name,
        r#type,
        location,
        tags,
        properties,
    } = rg;

    let mut h = Sha256::new();
    write_marker(&mut h, b"tenantless-etag-v1:resource-group");
    write_lp_str(&mut h, id);
    write_lp_str(&mut h, name);
    write_lp_str(&mut h, r#type);
    write_lp_str(&mut h, location);
    write_lp_json(&mut h, tags);
    write_lp_json(&mut h, properties);
    finish(h)
}

// ---- framing helpers (private) -----------------------------------------------------

/// A `std::io::Write` adapter that feeds every written byte straight into the reused
/// `Sha256` (no second serialized buffer). `write` never short-writes.
struct HashWriter<'a>(&'a mut Sha256);

impl Write for HashWriter<'_> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.update(buf);
        Ok(buf.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// A `std::io::Write` sink that only COUNTS bytes (allocates nothing, discards content).
/// Used to pre-measure a JSON field's serialized length for its length prefix without
/// materializing a second buffer.
struct CountingSink(u64);

impl Write for CountingSink {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0 += buf.len() as u64;
        Ok(buf.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// Length-prefix raw bytes: `u64_le(len) ++ bytes`.
fn write_lp_bytes(h: &mut Sha256, b: &[u8]) {
    h.update((b.len() as u64).to_le_bytes());
    h.update(b);
}

/// Length-prefix a UTF-8 string.
fn write_lp_str(h: &mut Sha256, s: &str) {
    write_lp_bytes(h, s.as_bytes());
}

/// The domain/version marker, itself length-prefixed so `:resource` can never be a raw
/// prefix of `:resource-group` in the byte stream.
fn write_marker(h: &mut Sha256, marker: &[u8]) {
    write_lp_bytes(h, marker);
}

/// Length-prefix a JSON `Value`'s canonical (sorted-key, compact) serialization, streamed
/// into the hasher with no intermediate buffer: one counting pass for the length, one
/// hashing pass for the bytes. `serde_json::to_writer` over an in-memory `Value` is
/// infallible except for the writer, and neither writer here ever errors.
fn write_lp_json(h: &mut Sha256, v: &Value) {
    let mut sink = CountingSink(0);
    serde_json::to_writer(&mut sink, v).expect("counting JSON never fails");
    h.update(sink.0.to_le_bytes());
    let mut hw = HashWriter(h);
    serde_json::to_writer(&mut hw, v).expect("hashing JSON never fails");
}

/// Optional string: `0x00` absent, or `0x01` + length-prefixed bytes present.
fn write_opt_str(h: &mut Sha256, s: &Option<String>) {
    match s {
        None => h.update([0x00]),
        Some(v) => {
            h.update([0x01]);
            write_lp_str(h, v);
        }
    }
}

/// Optional JSON value: `0x00` absent, or `0x01` + length-prefixed canonical JSON present.
fn write_opt_json(h: &mut Sha256, v: &Option<Value>) {
    match v {
        None => h.update([0x00]),
        Some(val) => {
            h.update([0x01]);
            write_lp_json(h, val);
        }
    }
}

/// Finalize the digest into the strong, quoted `"b-<lowercase-64-hex>"` token.
fn finish(h: Sha256) -> String {
    format!("\"b-{}\"", hex::encode(h.finalize()))
}

/// Outcome of a conditional-write precondition evaluation.
///
/// `Proceed` = the request may go on to mutate; `Failed` = the precondition was not
/// satisfied and the handler must return HTTP `412 PreconditionFailed`.
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
pub enum Precond {
    /// The precondition(s) held — the write may proceed.
    Proceed,
    /// A precondition was not satisfied — the handler returns `412`.
    Failed,
}

/// Evaluate the `If-Match` / `If-None-Match` optimistic-concurrency preconditions for a
/// write, per Phase-23 D-07 / D-15 / D-22.
///
/// * `if_match` / `if_none_match` are the RAW header values as received (may be
///   comma-separated lists, may carry surrounding/per-member whitespace, may carry a
///   weak validator `W/"…"`).
/// * `current` is the resource's server-derived **STRONG** ETag token (e.g. `"o-42"` /
///   `"b-<hex>"`), or `None` if the resource has no current live representation
///   (absent / tombstoned).
///
/// Both preconditions are evaluated independently and the result is `Proceed` **only if
/// both hold** — the `If-None-Match: "*"` case must never early-return past a conflicting
/// `If-Match` (D-22). Semantics:
/// * `If-None-Match` FAILS when a `*` member is present and the resource exists, OR when any
///   **strong** member byte-equals `current`. A weak validator never strong-matches.
/// * `If-Match` FAILS when the resource is absent (D-15 — `412`, evaluated before existence),
///   OR when neither a `*` member (which requires the resource to exist) nor any strong
///   member byte-equals `current`.
/// * A weak validator (`W/"…"`) can NEVER satisfy the strong comparison ARM uses, for either
///   header.
///
/// The comparison is byte-equality against the tokens produced by [`overlay_etag`] /
/// [`baseline_resource_etag`] — no re-derivation and no weak→strong normalization.
pub fn evaluate_precondition(
    if_match: Option<&str>,
    if_none_match: Option<&str>,
    current: Option<&str>,
) -> Precond {
    // If-None-Match precondition (passes vacuously when the header is absent).
    let if_none_match_ok = match if_none_match {
        None => true,
        Some(raw) => {
            let (wildcard, strong) = parse_etag_members(raw);
            // FAILS when a `*` member is present and the resource exists, OR when any
            // strong (non-weak) member byte-equals the current strong token.
            let fails =
                (wildcard && current.is_some()) || strong.iter().any(|m| Some(*m) == current);
            !fails
        }
    };

    // If-Match precondition (passes vacuously when the header is absent).
    let if_match_ok = match if_match {
        None => true,
        Some(raw) => {
            let (wildcard, strong) = parse_etag_members(raw);
            match current {
                // D-15: an absent resource can never satisfy an If-Match (incl. `*`) → 412,
                // evaluated before existence handling.
                None => false,
                // `*` requires existence (satisfied here); else any strong member must match.
                Some(cur) => wildcard || strong.contains(&cur),
            }
        }
    };

    if if_none_match_ok && if_match_ok {
        Precond::Proceed
    } else {
        Precond::Failed
    }
}

/// Split a raw `If-Match`/`If-None-Match` header value into its members (comma-separated),
/// trimming surrounding whitespace per member, and classify them. Returns
/// `(has_wildcard, strong_members)`: a `*` member sets the wildcard flag; a member beginning
/// with `W/` is a WEAK validator and is dropped (it can never satisfy the strong comparison
/// ARM uses); every other non-empty member is a strong validator token compared byte-for-byte.
fn parse_etag_members(raw: &str) -> (bool, Vec<&str>) {
    let mut wildcard = false;
    let mut strong = Vec::new();
    for member in raw.split(',') {
        let m = member.trim();
        if m.is_empty() {
            continue;
        }
        if m == "*" {
            wildcard = true;
        } else if m.starts_with("W/") {
            // weak validator — never a strong match; drop it.
        } else {
            strong.push(m);
        }
    }
    (wildcard, strong)
}

#[cfg(test)]
mod precond {
    use super::*;

    #[test]
    fn if_none_match_star_on_existing_fails() {
        // Create-guard: resource exists → 412.
        assert_eq!(
            evaluate_precondition(None, Some("*"), Some("\"o-1\"")),
            Precond::Failed
        );
    }

    #[test]
    fn if_none_match_star_on_absent_proceeds() {
        assert_eq!(
            evaluate_precondition(None, Some("*"), None),
            Precond::Proceed
        );
    }

    #[test]
    fn if_match_byte_equal_strong_proceeds() {
        assert_eq!(
            evaluate_precondition(Some("\"o-5\""), None, Some("\"o-5\"")),
            Precond::Proceed
        );
    }

    #[test]
    fn if_match_stale_fails() {
        assert_eq!(
            evaluate_precondition(Some("\"o-5\""), None, Some("\"o-6\"")),
            Precond::Failed
        );
    }

    #[test]
    fn if_match_on_absent_fails_412_not_404_before_existence() {
        // D-15: If-Match on an absent resource → 412, evaluated BEFORE existence handling
        // (so a stale/absent-target conditional write is 412, never 404).
        assert_eq!(
            evaluate_precondition(Some("\"o-5\""), None, None),
            Precond::Failed
        );
    }

    #[test]
    fn if_match_star_requires_existence() {
        assert_eq!(
            evaluate_precondition(Some("*"), None, Some("\"o-1\"")),
            Precond::Proceed
        );
        assert_eq!(
            evaluate_precondition(Some("*"), None, None),
            Precond::Failed
        );
    }

    #[test]
    fn no_headers_proceeds_unconditional() {
        assert_eq!(
            evaluate_precondition(None, None, Some("\"o-1\"")),
            Precond::Proceed
        );
        assert_eq!(evaluate_precondition(None, None, None), Precond::Proceed);
    }

    #[test]
    fn if_match_comma_list_matches_any_strong_member() {
        // D-22: any strong member matching current → proceed.
        assert_eq!(
            evaluate_precondition(Some("\"o-4\", \"o-5\""), None, Some("\"o-5\"")),
            Precond::Proceed
        );
        // None of the list members match → 412.
        assert_eq!(
            evaluate_precondition(Some("\"o-4\", \"o-5\""), None, Some("\"o-9\"")),
            Precond::Failed
        );
    }

    #[test]
    fn if_match_surrounding_and_per_member_whitespace_tolerated() {
        // D-22: surrounding whitespace on a single value…
        assert_eq!(
            evaluate_precondition(Some("  \"o-5\"  "), None, Some("\"o-5\"")),
            Precond::Proceed
        );
        // …and per-member whitespace inside a list.
        assert_eq!(
            evaluate_precondition(Some("  \"o-4\" ,  \"o-5\" "), None, Some("\"o-5\"")),
            Precond::Proceed
        );
    }

    #[test]
    fn weak_validator_never_satisfies_if_match() {
        // D-22: a weak validator W/"…" NEVER satisfies the strong comparison → 412.
        assert_eq!(
            evaluate_precondition(Some("W/\"o-5\""), None, Some("\"o-5\"")),
            Precond::Failed
        );
    }

    #[test]
    fn weak_validator_in_if_none_match_does_not_trip_guard() {
        // D-22: a weak tag does not strong-match, so the create/overwrite guard does NOT trip.
        assert_eq!(
            evaluate_precondition(None, Some("W/\"o-5\""), Some("\"o-5\"")),
            Precond::Proceed
        );
    }

    #[test]
    fn both_headers_are_each_honored_no_masking() {
        // D-22: If-None-Match:"*" must NOT mask a conflicting If-Match — the If-Match
        // mismatch is still honored (both resolve to Failed here; the point is the
        // If-None-Match early-return cannot skip the If-Match check).
        assert_eq!(
            evaluate_precondition(Some("\"o-5\""), Some("*"), Some("\"o-9\"")),
            Precond::Failed
        );
        // Both preconditions pass together → Proceed.
        assert_eq!(
            evaluate_precondition(Some("\"o-5\""), Some("\"o-1\""), Some("\"o-5\"")),
            Precond::Proceed
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::arm::{Resource, ResourceGroup, ResourceRow};
    use serde_json::json;
    use sqlx::types::Json;

    // ---- KAT: independently computed from the documented byte layout.
    // Python: sha256 over  LP(marker) LP(id) LP(name) LP(type) LP(location) LP_JSON(tags)
    //         OPT_JSON(sku=Some) OPT_STR(kind=Some) LP_JSON(properties)  — NOT a run capture.
    const RESOURCE_KAT: &str =
        "\"b-a561e9970236c6269bd736866bccfac2c0ad57f4cfec4a05547e1da51c0a5025\"";

    // ---- RG KAT: independently computed (Python hashlib) from the documented RG byte
    // layout  LP(marker=":resource-group") LP(id) LP(name) LP(type=const) LP(location)
    //         LP_JSON(tags) LP_JSON(properties={"provisioningState":...})  — not a capture.
    const RG_KAT: &str = "\"b-630082d45e610528d9bbc697feb3fd09adb75b72acbf4c8614396b19db58476a\"";

    fn kat_rg() -> ResourceGroup {
        ResourceGroup {
            id: "/subscriptions/s1/resourceGroups/rg1".into(),
            name: "rg1".into(),
            r#type: "Microsoft.Resources/resourceGroups".into(),
            location: "eastus".into(),
            tags: json!({"env": "prod"}),
            properties: json!({"provisioningState": "Succeeded"}),
        }
    }

    fn kat_resource() -> Resource {
        Resource {
            id: "/subscriptions/s1/resourceGroups/rg1/providers/Microsoft.Storage/storageAccounts/acct1".into(),
            name: "acct1".into(),
            r#type: "Microsoft.Storage/storageAccounts".into(),
            location: "eastus".into(),
            tags: json!({"env": "prod"}),
            sku: Some(json!({"name": "Standard_LRS"})),
            kind: Some("StorageV2".into()),
            properties: json!({"accessTier": "Hot"}),
        }
    }

    /// A `b-` token is always `"b-` + exactly 64 lowercase hex chars + closing quote,
    /// and is never weak/unquoted.
    fn assert_strong_b_token(tok: &str) {
        assert!(
            tok.starts_with("\"b-"),
            "not b-prefixed inside quotes: {tok}"
        );
        assert!(tok.ends_with('"'), "not quoted: {tok}");
        assert!(!tok.starts_with("W/"), "must never be weak: {tok}");
        let hex = &tok[3..tok.len() - 1];
        assert_eq!(hex.len(), 64, "b- hash must be 64 hex chars: {tok}");
        assert!(
            hex.chars()
                .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c)),
            "b- hash must be lowercase hex: {tok}"
        );
    }

    #[test]
    fn etag_overlay_format() {
        // Canonical decimal, prefix inside the quotes, no leading zeros, no hashing.
        assert_eq!(overlay_etag(1), "\"o-1\"");
        assert_eq!(overlay_etag(42), "\"o-42\"");
        // A large bigint revision formats as canonical decimal.
        assert_eq!(
            overlay_etag(9_007_199_254_740_993),
            "\"o-9007199254740993\""
        );
        // Never weak, always quoted with the prefix inside.
        let t = overlay_etag(7);
        assert!(t.starts_with("\"o-") && t.ends_with('"') && !t.starts_with("W/"));
    }

    #[test]
    fn etag_resource_kat_frozen() {
        let tok = baseline_resource_etag(&kat_resource());
        assert_eq!(tok, RESOURCE_KAT);
        assert_strong_b_token(&tok);
    }

    #[test]
    fn etag_resource_field_sensitivity() {
        let base = baseline_resource_etag(&kat_resource());

        // Every served field, when mutated, MUST change the hash.
        let mut r = kat_resource();
        r.id = "/subscriptions/s1/resourceGroups/rg1/providers/Microsoft.Storage/storageAccounts/OTHER".into();
        assert_ne!(baseline_resource_etag(&r), base, "id must be sensitive");

        let mut r = kat_resource();
        r.name = "other".into();
        assert_ne!(baseline_resource_etag(&r), base, "name must be sensitive");

        let mut r = kat_resource();
        r.r#type = "Microsoft.Storage/storageAccounts/blobServices".into();
        assert_ne!(baseline_resource_etag(&r), base, "type must be sensitive");

        let mut r = kat_resource();
        r.location = "westus".into();
        assert_ne!(
            baseline_resource_etag(&r),
            base,
            "location must be sensitive"
        );

        let mut r = kat_resource();
        r.tags = json!({"env": "dev"});
        assert_ne!(
            baseline_resource_etag(&r),
            base,
            "tags value must be sensitive"
        );

        // sku: value change, and present-vs-absent.
        let mut r = kat_resource();
        r.sku = Some(json!({"name": "Premium_LRS"}));
        assert_ne!(
            baseline_resource_etag(&r),
            base,
            "sku value must be sensitive"
        );
        let mut r = kat_resource();
        r.sku = None;
        let sku_absent = baseline_resource_etag(&r);
        assert_ne!(sku_absent, base, "sku present-vs-absent must be sensitive");

        // kind: value change, and present-vs-absent.
        let mut r = kat_resource();
        r.kind = Some("StorageV3".into());
        assert_ne!(
            baseline_resource_etag(&r),
            base,
            "kind value must be sensitive"
        );
        let mut r = kat_resource();
        r.kind = None;
        let kind_absent = baseline_resource_etag(&r);
        assert_ne!(
            kind_absent, base,
            "kind present-vs-absent must be sensitive"
        );

        // properties value.
        let mut r = kat_resource();
        r.properties = json!({"accessTier": "Cool"});
        assert_ne!(
            baseline_resource_etag(&r),
            base,
            "properties must be sensitive"
        );

        // absent-sku and absent-kind are themselves distinct (0x00 framing is unambiguous).
        assert_ne!(sku_absent, kind_absent);

        // The 0x00/0x01 option framing distinguishes an absent field from an empty one:
        // sku = Some({}) must differ from sku = None.
        let mut r = kat_resource();
        r.sku = Some(json!({}));
        assert_ne!(
            baseline_resource_etag(&r),
            sku_absent,
            "Some({{}}) != None for sku"
        );
    }

    #[test]
    fn etag_resource_unserved_insensitivity() {
        // provisioning_state and managed_by are columns on synthetic.resources (sql/001)
        // but appear in NEITHER the ResourceRow projection NOR the Resource DTO — so no
        // value of either can ever enter the preimage. The exclusion is a COMPILE-TIME
        // fact: ResourceRow has no such fields to set, and baseline_resource_etag's
        // exhaustive `let Resource { .. }` (no `..`) covers exactly the 8 served fields.
        // Two rows representing the same served resource (regardless of whatever unserved
        // DB state exists behind them) therefore hash identically.
        let row = |props: Value| ResourceRow {
            id:
                "/subscriptions/s1/resourceGroups/rg1/providers/Microsoft.Storage/storageAccounts/a"
                    .into(),
            name: "a".into(),
            r#type: "Microsoft.Storage/storageAccounts".into(),
            location: "eastus".into(),
            tags: Json(json!({"env": "prod"})),
            sku: None,
            kind: None,
            properties: Json(props),
        };
        let a: Resource = row(json!({"accessTier": "Hot"})).into();
        let b: Resource = row(json!({"accessTier": "Hot"})).into();
        assert_eq!(baseline_resource_etag(&a), baseline_resource_etag(&b));
    }

    #[test]
    fn etag_null_eq_empty_properties() {
        // From<ResourceRow> coalesces a JSON-null properties column to {} (arm.rs:162-165),
        // so a null-properties row and a {}-properties row serve identical bytes → equal hash.
        let row = |props: Value| ResourceRow {
            id:
                "/subscriptions/s1/resourceGroups/rg1/providers/Microsoft.Storage/storageAccounts/a"
                    .into(),
            name: "a".into(),
            r#type: "Microsoft.Storage/storageAccounts".into(),
            location: "eastus".into(),
            tags: Json(json!({})),
            sku: None,
            kind: None,
            properties: Json(props),
        };
        let null_props: Resource = row(Value::Null).into();
        let empty_props: Resource = row(json!({})).into();
        assert_eq!(
            baseline_resource_etag(&null_props),
            baseline_resource_etag(&empty_props),
            "properties=null and properties={{}} must hash equal"
        );
    }

    #[test]
    fn etag_key_order_canary() {
        // RECURSIVE canonicalization proof: two inputs differing in JSON key order
        // at BOTH the top level AND inside a NESTED object must hash byte-equal. Parsing a
        // shuffled-key JSON string yields a BTreeMap-backed Value (preserve_order OFF), which
        // serde_json::to_writer emits with recursively-sorted keys. A transitive
        // preserve_order feature-flip would make these two differ and trip this canary.
        let props_a: Value =
            serde_json::from_str(r#"{"z":1,"nested":{"y":2,"x":1},"a":3}"#).unwrap();
        let props_b: Value =
            serde_json::from_str(r#"{"a":3,"nested":{"x":1,"y":2},"z":1}"#).unwrap();
        let tags_a: Value = serde_json::from_str(r#"{"team":"core","env":"prod"}"#).unwrap();
        let tags_b: Value = serde_json::from_str(r#"{"env":"prod","team":"core"}"#).unwrap();

        let make = |tags: Value, props: Value| Resource {
            id:
                "/subscriptions/s1/resourceGroups/rg1/providers/Microsoft.Storage/storageAccounts/a"
                    .into(),
            name: "a".into(),
            r#type: "Microsoft.Storage/storageAccounts".into(),
            location: "eastus".into(),
            tags,
            sku: None,
            kind: None,
            properties: props,
        };
        let a = make(tags_a, props_a);
        let b = make(tags_b, props_b);
        assert_eq!(
            baseline_resource_etag(&a),
            baseline_resource_etag(&b),
            "key order (top-level AND nested) must not change the hash"
        );
    }

    #[test]
    fn etag_rg_kat_frozen() {
        let tok = baseline_rg_etag(&kat_rg());
        assert_eq!(tok, RG_KAT);
        assert_strong_b_token(&tok);
    }

    #[test]
    fn etag_rg_field_sensitivity() {
        let base = baseline_rg_etag(&kat_rg());

        let mut rg = kat_rg();
        rg.id = "/subscriptions/s1/resourceGroups/OTHER".into();
        assert_ne!(baseline_rg_etag(&rg), base, "id must be sensitive");

        let mut rg = kat_rg();
        rg.name = "other".into();
        assert_ne!(baseline_rg_etag(&rg), base, "name must be sensitive");

        let mut rg = kat_rg();
        rg.location = "westus".into();
        assert_ne!(baseline_rg_etag(&rg), base, "location must be sensitive");

        let mut rg = kat_rg();
        rg.tags = json!({"env": "dev"});
        assert_ne!(baseline_rg_etag(&rg), base, "tags must be sensitive");

        // The served provisioningState inside `properties` IS part of the RG representation.
        let mut rg = kat_rg();
        rg.properties = json!({"provisioningState": "Updating"});
        assert_ne!(
            baseline_rg_etag(&rg),
            base,
            "properties/provisioningState must be sensitive"
        );
    }

    #[test]
    fn etag_rg_domain_distinct() {
        // A resource and an RG that share id/name/location/tags derive DIFFERENT b- hashes:
        // the distinct version markers (+ the RG's served const type participating in the
        // preimage) make the two domains non-colliding. No derived token is weak/unquoted.
        let shared_id = "/subscriptions/s1/resourceGroups/rg1";
        let res = Resource {
            id: shared_id.into(),
            name: "rg1".into(),
            r#type: "Microsoft.Resources/resourceGroups".into(),
            location: "eastus".into(),
            tags: json!({"env": "prod"}),
            sku: None,
            kind: None,
            properties: json!({"provisioningState": "Succeeded"}),
        };
        let rg = ResourceGroup {
            id: shared_id.into(),
            name: "rg1".into(),
            r#type: "Microsoft.Resources/resourceGroups".into(),
            location: "eastus".into(),
            tags: json!({"env": "prod"}),
            properties: json!({"provisioningState": "Succeeded"}),
        };
        let res_tok = baseline_resource_etag(&res);
        let rg_tok = baseline_rg_etag(&rg);
        assert_ne!(res_tok, rg_tok, "resource and RG domains must not collide");
        assert_strong_b_token(&res_tok);
        assert_strong_b_token(&rg_tok);
    }
}
