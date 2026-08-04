//! `GET /subscriptions/{sub}/resources` — keyset-paginated ARM resource list with
//! `$top` clamp + opaque `$skiptoken` continuation + absolute `nextLink` (MOCK-03,
//! MOCK-08), plus OData `$filter` (MOCK-06, D-03: both list endpoints).
//!
//! Mirrors `list_resource_groups`: keyset over the `id` PK (`WHERE subscription_id
//! = $1 AND ($2 IS NULL OR id > $2) ORDER BY id LIMIT $3`, `$3 = clamp_top + 1`), the
//! surplus row driving `nextLink` emission. `{sub}` is parsed as `Uuid` and the
//! decoded cursor is `.bind()`-bound — never spliced into SQL (T-03-06/T-03-09). The
//! explicit column projection keeps the response shape stable (no `SELECT *`).
//!
//! `$filter` is the one place this crate builds a list query string at runtime. The
//! dynamic conjunct is **placeholders-only**: [`filter::Filter::to_sql`] emits a
//! fragment whose only non-column-name tokens are `$N` placeholders + boolean
//! keywords + parens, and every user literal flows through the parallel bound-args
//! `Vec` into the `for a in args` bind loop — never `format!`-ed into SQL text
//! (T-04-10, extending T-03-09/10). A malformed `$filter` short-circuits to a 400
//! via `?` BEFORE any SQL is built (D-04). The injection-safety invariant is pinned
//! directly here by [`tests::filter_conjunct_is_placeholders_only_even_with_sql_metachars`].

use crate::{
    arm::{ListResponse, Resource, ResourceRow},
    error::ApiError,
    filter::{self, Filter},
    pagination::{PageParams, clamp_top, decode_token, next_link, split_page},
    state::AppState,
};
use axum::{
    Json,
    extract::{Path, Query, State},
};
use uuid::Uuid;

/// Build the dynamic `$filter` WHERE conjunct for a list query.
///
/// Returns `(where_extra, args)` where `where_extra` is either the empty string (no
/// `$filter`) or `" AND (<fragment>)"` with `<fragment>` being a **placeholders-only**
/// SQL fragment (column names + `$N` + boolean keywords + parens only — never a user
/// literal). `args` carries every literal in bind order. `next` is the next free
/// placeholder index, seeded PAST the handler's fixed binds (`4` unscoped / `5`
/// rg-scoped); it is advanced as args are pushed so `#($N) == args.len()`.
///
/// This is the per-handler injection-safety boundary (T-04-10): because the fragment
/// is assembled solely from `filter::Filter::to_sql`'s closed column `match` + `$N`
/// tokens, no `value`/`key`/`field` text ever reaches the SQL string.
fn filter_conjunct(parsed: &Option<Filter>, next: i32) -> (String, Vec<String>) {
    let mut next = next;
    let mut args = Vec::<String>::new();
    let where_extra = match parsed {
        Some(f) => format!(" AND ({})", f.to_sql(&mut next, &mut args)),
        None => String::new(),
    };
    (where_extra, args)
}

/// List a subscription's resources in the ARM envelope, keyset-paginated by `id`
/// with `$top` clamp and opaque `$skiptoken` continuation, optionally `$filter`ed.
pub async fn list_resources(
    State(state): State<AppState>,
    Path(sub): Path<Uuid>,
    Query(params): Query<PageParams>,
) -> Result<Json<ListResponse<Resource>>, ApiError> {
    let top = clamp_top(params.top);
    let cursor = params.skiptoken.as_deref().map(decode_token).transpose()?;

    // Parse `$filter` BEFORE building any SQL — a parse error short-circuits to a
    // fixed-string 400 via `?` before a query ever runs (D-04, T-04-11).
    let parsed = params.filter.as_deref().map(filter::parse).transpose()?;
    // Fixed binds are $1 sub, $2 cursor, $3 top+1 → filter placeholders start at $4.
    let (where_extra, filter_args) = filter_conjunct(&parsed, 4);

    let sql = format!(
        "SELECT id, name, type, location, tags, sku, kind, properties
         FROM synthetic.resources
         WHERE subscription_id = $1 AND ($2::text IS NULL OR id > $2) AND drift_deleted_at IS NULL{where_extra}
         ORDER BY id
         LIMIT $3"
    );

    let mut q = sqlx::query_as::<_, ResourceRow>(&sql)
        .bind(sub)
        .bind(cursor)
        .bind(top + 1);
    // Dynamic bind loop — the SQL text contains only $N tokens; every literal is bound.
    for a in filter_args {
        q = q.bind(a);
    }
    let rows = q.fetch_all(&state.pool).await?;

    let (page, next_token) = split_page(rows, top, |r| r.id.as_str());
    let value: Vec<Resource> = page.into_iter().map(Resource::from).collect();

    let mut response = ListResponse::new(value);
    if let Some(tok) = next_token {
        let path = format!("/subscriptions/{sub}/resources");
        response.next_link = Some(next_link(
            &state.base_url,
            &path,
            top,
            &tok,
            params.api_version.as_deref(),
            params.filter.as_deref(),
        ));
    }
    Ok(Json(response))
}

/// List a single resource group's resources in the ARM envelope (MOCK-04), optionally
/// `$filter`ed (D-03).
///
/// Identical to [`list_resources`] plus one bound predicate: `AND
/// lower(resource_group_name) = lower($4)`. The comparison is case-insensitive so a
/// differently-cased `{rg}` in the request resolves to the canonically-cased stored
/// group — the same rule the resource-detail lookup applies to the whole id
/// (`lower(id) = lower($1)`), so a scanner that lists an RG's resources and one that
/// fetches a resource by id agree on which RG a path names. `{rg}` is bound as a
/// parameter — never spliced into SQL (T-03-10). An unknown `{sub}` or `{rg}` simply
/// yields zero rows, so the envelope is `{ "value": [] }` (no existence pre-check, no
/// 404 — locked decision). Because `rg` keeps `$4`, the `$filter` placeholders seed at
/// `$5`.
pub async fn list_rg_resources(
    State(state): State<AppState>,
    Path((sub, rg)): Path<(Uuid, String)>,
    Query(params): Query<PageParams>,
) -> Result<Json<ListResponse<Resource>>, ApiError> {
    let top = clamp_top(params.top);
    let cursor = params.skiptoken.as_deref().map(decode_token).transpose()?;

    // Parse `$filter` BEFORE building any SQL (D-04, T-04-11).
    let parsed = params.filter.as_deref().map(filter::parse).transpose()?;
    // Fixed binds are $1 sub, $2 cursor, $3 top+1, $4 rg → filter placeholders start at $5.
    let (where_extra, filter_args) = filter_conjunct(&parsed, 5);

    let sql = format!(
        "SELECT id, name, type, location, tags, sku, kind, properties
         FROM synthetic.resources
         WHERE subscription_id = $1 AND ($2::text IS NULL OR id > $2)
           AND lower(resource_group_name) = lower($4) AND drift_deleted_at IS NULL{where_extra}
         ORDER BY id
         LIMIT $3"
    );

    let mut q = sqlx::query_as::<_, ResourceRow>(&sql)
        .bind(sub)
        .bind(cursor)
        .bind(top + 1)
        .bind(&rg);
    // Dynamic bind loop — the SQL text contains only $N tokens; every literal is bound.
    for a in filter_args {
        q = q.bind(a);
    }
    let rows = q.fetch_all(&state.pool).await?;

    let (page, next_token) = split_page(rows, top, |r| r.id.as_str());
    let value: Vec<Resource> = page.into_iter().map(Resource::from).collect();

    let mut response = ListResponse::new(value);
    if let Some(tok) = next_token {
        let path = format!("/subscriptions/{sub}/resourceGroups/{rg}/resources");
        response.next_link = Some(next_link(
            &state.base_url,
            &path,
            top,
            &tok,
            params.api_version.as_deref(),
            params.filter.as_deref(),
        ));
    }
    Ok(Json(response))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The handler's injection-safety invariant, pinned DB-free (T-04-10): for a
    /// filter whose tag value carries a SQL metacharacter (`'; DROP`), the WHERE
    /// conjunct fragment must contain ONLY `$N` placeholders, column names, boolean
    /// keywords, and parens — NEVER the user literal — while that literal appears
    /// verbatim in the returned bound `args`.
    #[test]
    fn filter_conjunct_is_placeholders_only_even_with_sql_metachars() {
        // A representative compound filter: a paired tag whose VALUE is an injection
        // attempt, AND-composed with a resourceType scalar. The `''` escapes the quote
        // so the parser accepts the literal as plain text (the dangerous payload).
        let attack = "'; DROP TABLE synthetic.resources;--";
        let escaped = attack.replace('\'', "''");
        let input = format!(
            "tagName eq 'env' and tagValue eq '{escaped}' and resourceType eq 'Microsoft.Storage/storageAccounts'"
        );
        let parsed = Some(filter::parse(&input).expect("escaped-injection filter must parse"));

        // Seed past the unscoped handler's fixed binds ($1..$3) → filter starts at $4.
        let (where_extra, args) = filter_conjunct(&parsed, 4);

        // The fragment is " AND (<placeholders-only>)".
        assert!(
            where_extra.starts_with(" AND ("),
            "conjunct must be a parenthesized AND-extension, got {where_extra:?}"
        );

        // The user literal NEVER appears in the SQL text — neither the raw attack nor
        // any recognizable splice fragment of it.
        assert!(
            !where_extra.contains(attack),
            "fragment must not contain the raw attack literal: {where_extra:?}"
        );
        assert!(
            !where_extra.contains("DROP"),
            "fragment must not contain any spliced SQL keyword from the literal: {where_extra:?}"
        );
        assert!(
            !where_extra.contains(';'),
            "fragment must not contain a statement separator from the literal: {where_extra:?}"
        );

        // The literal IS carried — verbatim — as a bound arg (data, not code).
        assert!(
            args.iter().any(|a| a == attack),
            "the attack literal must be carried verbatim in the bound args: {args:?}"
        );

        // Structural placeholder seeding: filter binds start at $4 (past $1..$3) and the
        // placeholder count equals the number of bound args (no orphan placeholders).
        let placeholder_count = where_extra.matches('$').count();
        assert_eq!(
            placeholder_count,
            args.len(),
            "every $N must have exactly one bound arg (fragment: {where_extra:?}, args: {args:?})"
        );
        // tagName + tagValue (2) + resourceType (1) = 3 binds, seeded $4,$5,$6.
        assert_eq!(
            args.len(),
            3,
            "expected 3 bound literals for this compound filter"
        );
        assert!(
            where_extra.contains("$4"),
            "first filter placeholder must be $4: {where_extra:?}"
        );
        assert!(
            where_extra.contains("$6"),
            "last filter placeholder must be $6: {where_extra:?}"
        );

        // The fragment's only column/operator tokens are the trusted, closed-match ones.
        assert!(
            where_extra.contains("tags ->>"),
            "tag pair maps to the JSONB ->> operator"
        );
        assert!(
            where_extra.contains("type ="),
            "resourceType maps to the `type` column"
        );
    }

    /// No `$filter` → no conjunct, no args (the keyset query is unchanged).
    #[test]
    fn filter_conjunct_is_empty_when_no_filter() {
        let (where_extra, args) = filter_conjunct(&None, 4);
        assert_eq!(
            where_extra, "",
            "absent filter contributes no WHERE conjunct"
        );
        assert!(args.is_empty(), "absent filter contributes no bound args");
    }

    /// The rg-scoped handler seeds the filter at $5 (rg holds $4) so a filtered scoped
    /// query never collides placeholders with the `lower(resource_group_name) = lower($4)`
    /// bind.
    #[test]
    fn filter_conjunct_seeds_at_five_for_scoped_handler() {
        let parsed = Some(filter::parse("location eq 'eastus'").expect("parse"));
        let (where_extra, args) = filter_conjunct(&parsed, 5);
        assert_eq!(where_extra, " AND (location = $5)");
        assert_eq!(args, vec!["eastus".to_string()]);
    }
}
