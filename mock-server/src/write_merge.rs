//! Pure, DB-free write-plane logic primitives for the generic ARM write plane.
//!
//! Everything here is a pure `serde_json`/`std`-only transform with no DB and no HTTP
//! surface — the highest-value TDD units the Phase-23 write handlers consume verbatim.
//!
//! # Contents
//! * [`two_level_merge`] — the D-01 two-level PATCH merge (explicitly **NOT** RFC 7386/7396:
//!   an explicit JSON `null` is STORED, never interpreted as a key deletion).
//! * `parse_segments` / `is_descendant` / `descendant_ids` — the D-09 parsed-id
//!   nested-descendant set (segment-granular containment, never a raw string prefix, so
//!   `servers/s1` can never sweep the sibling `servers/s10`).

use serde_json::Value;

/// Apply a two-level shallow PATCH merge of `patch` into `current`, per Phase-23 D-01.
///
/// This is deliberately **NOT** RFC 7386/7396 JSON Merge Patch:
/// * Top-level keys present in `patch` overwrite the corresponding key in `current`;
///   top-level keys absent from `patch` are preserved (`sku`, `kind`, `identity`, … survive
///   an omitted-key PATCH).
/// * The `"properties"` key merges **one level deep** when BOTH sides are JSON objects:
///   present property keys overwrite, absent property keys are preserved.
/// * Every other top-level key — including `"tags"` — and all arrays/scalars are replaced
///   **wholesale** (no element/array merge).
/// * An explicit JSON `null` is **stored** as the value; it never deletes a key.
///
/// If either side is not a JSON object, the merge degrades to a wholesale replacement of
/// `*current` with a clone of `patch` (there is no object structure to merge into).
///
/// This function performs ONLY the structural D-01 merge. Forcing server-owned fields
/// (`id`/`name`/`type`, `properties.provisioningState = "Succeeded"`) is the handler's job
/// AFTER the merge, not this function's.
pub fn two_level_merge(current: &mut Value, patch: &Value) {
    // Both sides must be JSON objects to merge structurally; otherwise the patch
    // wholesale-replaces the current value (there is nothing to merge into).
    let (Value::Object(cur_map), Value::Object(patch_map)) = (&mut *current, patch) else {
        *current = patch.clone();
        return;
    };

    for (key, patch_val) in patch_map {
        if key == "properties" {
            // "properties" merges ONE level deep — but only when both the existing and the
            // incoming values are objects. Any other combination replaces wholesale.
            if let (Some(Value::Object(cur_props)), Value::Object(patch_props)) =
                (cur_map.get_mut(key), patch_val)
            {
                merge_one_level(cur_props, patch_props);
                continue;
            }
        }
        // Every other top-level key (incl. "tags"), arrays, scalars, and an explicit
        // JSON null all REPLACE wholesale. null is stored, never treated as a deletion.
        cur_map.insert(key.clone(), patch_val.clone());
    }
}

/// Merge `patch` into `current` a single level deep: each present key in `patch` overwrites
/// the same key in `current` (arrays/scalars/objects/null all replace wholesale — no
/// recursion); keys absent from `patch` are preserved. Used for the D-01 `"properties"` merge.
fn merge_one_level(
    current: &mut serde_json::Map<String, Value>,
    patch: &serde_json::Map<String, Value>,
) {
    for (key, val) in patch {
        current.insert(key.clone(), val.clone());
    }
}

/// Parse a canonical ARM resource id into its ordered, lowercased path segments.
///
/// Splits on `/`, drops empty segments (leading slash / doubled slashes), and lowercases
/// each segment so containment comparisons are case-insensitive (D-08 lookup semantics).
/// e.g. `.../providers/Microsoft.Sql/servers/S1/databases/D1` →
/// `["subscriptions","<sub>","resourcegroups","<rg>","providers","microsoft.sql",
///   "servers","s1","databases","d1"]`.
pub fn parse_segments(id: &str) -> Vec<String> {
    id.split('/')
        .filter(|s| !s.is_empty())
        .map(|s| s.to_lowercase())
        .collect()
}

/// True iff `candidate_segments` is a **strict** descendant of `target_segments` at
/// **segment granularity** — i.e. the candidate has strictly more segments AND the
/// target's full segment list is a prefix of the candidate's.
///
/// This is a segment-list prefix, NEVER a raw string prefix: `servers/s1` must not catch the
/// sibling-lookalike `servers/s10` (D-09, RESEARCH Pitfall 5). A resource is never its own
/// descendant (the cascade tombstones the target separately).
pub fn is_descendant(target_segments: &[String], candidate_segments: &[String]) -> bool {
    // Strict: the candidate must have MORE segments (a resource is never its own descendant),
    // and the target's full segment list must be a prefix of the candidate's. Comparing
    // whole segments (never a raw substring) is what defeats the `s1`/`s10` sibling trap.
    candidate_segments.len() > target_segments.len()
        && candidate_segments[..target_segments.len()] == *target_segments
}

/// Return the subset of `candidate_ids` whose parsed segments make them a strict
/// nested descendant of `target_id` (per [`is_descendant`]). Original id strings are
/// returned verbatim (casing preserved); only the comparison is case-insensitive.
pub fn descendant_ids(target_id: &str, candidate_ids: &[String]) -> Vec<String> {
    let target_segments = parse_segments(target_id);
    candidate_ids
        .iter()
        .filter(|c| is_descendant(&target_segments, &parse_segments(c)))
        .cloned()
        .collect()
}

#[cfg(test)]
mod merge {
    use super::*;
    use serde_json::json;

    #[test]
    fn tags_replaced_wholesale_and_absent_top_level_key_preserved() {
        let mut current = json!({"tags": {"b": 2}, "sku": {"name": "S"}});
        two_level_merge(&mut current, &json!({"tags": {"a": 1}}));
        assert_eq!(current["tags"], json!({"a": 1}), "tags replaced wholesale");
        assert_eq!(
            current["sku"],
            json!({"name": "S"}),
            "absent top-level key (sku) preserved"
        );
    }

    #[test]
    fn properties_merge_one_level() {
        let mut current = json!({"properties": {"x": 0, "y": 9}});
        two_level_merge(&mut current, &json!({"properties": {"x": 1}}));
        assert_eq!(
            current["properties"],
            json!({"x": 1, "y": 9}),
            "properties merges one level (x overwritten, y preserved)"
        );
    }

    #[test]
    fn null_in_properties_is_stored_not_deleted() {
        // Pin the non-RFC-7396 rule: null STORES, it does not delete.
        let mut current = json!({"properties": {"x": 0}});
        two_level_merge(&mut current, &json!({"properties": {"x": null}}));
        assert!(
            current["properties"]["x"].is_null(),
            "properties.x must be JSON null PRESENT, not absent"
        );
        assert!(
            current["properties"].as_object().unwrap().contains_key("x"),
            "properties.x key must still be present"
        );
    }

    #[test]
    fn null_top_level_key_is_stored_not_deleted() {
        let mut current = json!({"kind": "StorageV2", "location": "eastus"});
        two_level_merge(&mut current, &json!({"kind": null}));
        assert!(
            current.as_object().unwrap().contains_key("kind"),
            "top-level kind key must still be present"
        );
        assert!(
            current["kind"].is_null(),
            "top-level kind must be JSON null"
        );
    }

    #[test]
    fn arrays_in_properties_replaced_wholesale() {
        let mut current = json!({"properties": {"arr": [1, 2, 3]}});
        two_level_merge(&mut current, &json!({"properties": {"arr": [1]}}));
        assert_eq!(
            current["properties"]["arr"],
            json!([1]),
            "arrays replace wholesale (no element merge)"
        );
    }

    #[test]
    fn empty_patch_leaves_current_unchanged() {
        let mut current = json!({"tags": {"b": 2}, "properties": {"x": 0}, "kind": "K"});
        let before = current.clone();
        two_level_merge(&mut current, &json!({}));
        assert_eq!(current, before, "empty patch is a no-op");
    }
}

#[cfg(test)]
mod descendant {
    use super::*;

    const SUB: &str = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg1";
    // target: .../providers/Microsoft.Sql/servers/s1
    fn server_s1() -> String {
        format!("{SUB}/providers/Microsoft.Sql/servers/s1")
    }
    // .../servers/s1/databases/d1 (genuine descendant of s1)
    fn server_s1_db_d1() -> String {
        format!("{SUB}/providers/Microsoft.Sql/servers/s1/databases/d1")
    }
    // .../servers/s10 (sibling LOOKALIKE — must never match s1)
    fn server_s10() -> String {
        format!("{SUB}/providers/Microsoft.Sql/servers/s10")
    }
    fn unrelated() -> String {
        format!("{SUB}/providers/Microsoft.Storage/storageAccounts/acct1")
    }

    #[test]
    fn parse_segments_lowercases_and_orders() {
        let segs = parse_segments(&server_s1_db_d1());
        // Ordered, lowercased, empties dropped.
        assert_eq!(segs[0], "subscriptions");
        assert!(segs.contains(&"servers".to_string()));
        assert!(segs.contains(&"s1".to_string()));
        assert!(segs.contains(&"databases".to_string()));
        assert!(segs.contains(&"d1".to_string()));
        // Provider namespace is lowercased.
        assert!(segs.contains(&"microsoft.sql".to_string()));
        // Ordering: servers precedes s1 precedes databases precedes d1.
        let pos = |s: &str| segs.iter().position(|x| x == s).unwrap();
        assert!(pos("servers") < pos("s1"));
        assert!(pos("s1") < pos("databases"));
        assert!(pos("databases") < pos("d1"));
        // No empty segments from the leading slash.
        assert!(segs.iter().all(|s| !s.is_empty()));
    }

    #[test]
    fn genuine_descendant_matches() {
        let t = parse_segments(&server_s1());
        let c = parse_segments(&server_s1_db_d1());
        assert!(
            is_descendant(&t, &c),
            "s1/databases/d1 IS a descendant of s1"
        );
    }

    #[test]
    fn sibling_lookalike_s10_is_not_a_descendant_of_s1() {
        let t = parse_segments(&server_s1());
        let c = parse_segments(&server_s10());
        assert!(
            !is_descendant(&t, &c),
            "servers/s10 must NOT be a descendant of servers/s1 (segment-parse, not string prefix)"
        );
        // The canonical bug this guards: a raw string-prefix test WOULD wrongly include s10.
        assert!(
            server_s10()
                .to_lowercase()
                .starts_with(&server_s1().to_lowercase()),
            "str::starts_with would wrongly include s10 — which is exactly why is_descendant \
             must be segment-parsed, not string-prefixed"
        );
    }

    #[test]
    fn a_resource_is_not_its_own_descendant() {
        let t = parse_segments(&server_s1());
        assert!(
            !is_descendant(&t, &t),
            "a resource is not its own descendant (cascade tombstones the target separately)"
        );
    }

    #[test]
    fn descendant_ids_returns_only_genuine_descendants() {
        let candidates = vec![server_s1_db_d1(), server_s10(), unrelated()];
        let got = descendant_ids(&server_s1(), &candidates);
        assert_eq!(
            got,
            vec![server_s1_db_d1()],
            "only servers/s1/databases/d1 is a descendant; s10 and the storage acct are excluded"
        );
    }

    #[test]
    fn matching_is_case_insensitive() {
        // Target with different casing than the candidate must still match.
        let target = format!("{SUB}/providers/Microsoft.Sql/Servers/S1");
        let candidate = server_s1_db_d1(); // .../servers/s1/databases/d1 (lowercase)
        let t = parse_segments(&target);
        let c = parse_segments(&candidate);
        assert!(
            is_descendant(&t, &c),
            "Servers/S1 target must match servers/s1/... candidate (case-insensitive)"
        );
        // And through descendant_ids too.
        let got = descendant_ids(&target, std::slice::from_ref(&candidate));
        assert_eq!(got, vec![candidate]);
    }
}
