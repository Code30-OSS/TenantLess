//! `GET .../Microsoft.Authorization/{roleDefinitions,roleAssignments}` — the RBAC
//! data plane (IAM-03, api-version 2022-04-01).
//!
//! Two surfaces:
//!   * **roleDefinitions** — a STATIC built-in catalogue served straight from a Rust
//!     `const` ([`BUILTIN_ROLE_DEFINITIONS`]), cloning the `cost.rs` `SERVICE_MAP`
//!     constant idiom. This is the Pitfall-3 cross-language constant: it MUST stay
//!     byte-identical (GUID + roleName) to `identity.py`'s `BUILTIN_ROLE_DEFINITIONS`,
//!     pinned by the `role_def_catalogue_agrees` integration test.
//!   * **roleAssignments** — a `$N`-bound sqlx read of `synthetic.role_assignments`
//!     (the cost.rs bind-never-splice idiom, project SQL-injection bar), mapped into
//!     the verified `{value:[{name,type,id,properties{...}}]}` envelope.
//!
//! All three routes register INSIDE the `arm` router (alongside the cost routes), so
//! they inherit the any-Bearer scanner contract + the `--enforce-auth` swap. The
//! response DTOs are OWN `#[derive(Serialize)]` shapes (the deliberate non-reuse of
//! `arm::ListResponse` where the shape differs, RESEARCH Q3 / cost.rs precedent).

use crate::{
    error::ApiError,
    pagination::{PageParams, clamp_top, cursor_uuid_from_token, encode_token, next_link},
    state::AppState,
};
use axum::{
    Json,
    extract::{Path, Query, State},
};
use serde::Serialize;
use sqlx::Row;
use uuid::Uuid;

// ---------------------------------------------------------------------------------
// Static built-in roleDefinition catalogue (the cost.rs SERVICE_MAP constant idiom).
// CROSS-LANGUAGE CONSTANT (Pitfall 3): the (guid, roleName) set is byte-identical to
// identity.py BUILTIN_ROLE_DEFINITIONS. Owner/Contributor/Reader are VERIFIED; the
// rest provide the specialized over-privilege signal (D-04/D-05).
// ---------------------------------------------------------------------------------

/// One built-in roleDefinition entry. Action lists are `&'static [&'static str]` so the
/// whole catalogue is a compile-time constant — zero DB, zero generation pass (the
/// "Don't Hand-Roll: built-in role catalogue" decision).
struct BuiltinRole {
    guid: &'static str,
    role_name: &'static str,
    description: &'static str,
    actions: &'static [&'static str],
    not_actions: &'static [&'static str],
    data_actions: &'static [&'static str],
    not_data_actions: &'static [&'static str],
}

/// The tenant-scoped roleDefinitionId prefix used INSIDE an assignment (no
/// `/subscriptions/{sub}` prefix — RESEARCH Q3). The GET-at-scope id form prefixes the
/// subscription separately. Served assignments emit `role_definition_id` verbatim from
/// the DB (already stored in this tenant-scoped form by the writer), so this constant is
/// only referenced by the shape unit test that pins the casing/scoping contract.
#[cfg(test)]
const ROLE_DEF_ID_PREFIX: &str = "/providers/Microsoft.Authorization/roleDefinitions/";

/// The eight built-in roleDefinitions served by this mock (RESEARCH Q3). The
/// (guid, roleName) set is byte-identical to `identity.py BUILTIN_ROLE_DEFINITIONS`
/// (Pitfall 3); descriptions/permissions are representative of the real Azure
/// built-ins. Owner/Contributor/Reader are VERIFIED; the rest carry the specialized
/// over-privilege signal (D-04/D-05).
const BUILTIN_ROLE_DEFINITIONS: &[BuiltinRole] = &[
    BuiltinRole {
        guid: "8e3af657-bb00-4899-acbc-f0f7f5db61aa",
        role_name: "Owner",
        description: "Grants full access to manage all resources, including the ability to assign roles in Azure RBAC.",
        actions: &["*"],
        not_actions: &[],
        data_actions: &[],
        not_data_actions: &[],
    },
    BuiltinRole {
        guid: "b24988ac-6180-42a0-ab88-20f7382dd24c",
        role_name: "Contributor",
        description: "Grants full access to manage all resources, but does not allow you to assign roles in Azure RBAC, manage assignments in Azure Blueprints, or share image galleries.",
        actions: &["*"],
        not_actions: &[
            "Microsoft.Authorization/*/Delete",
            "Microsoft.Authorization/*/Write",
            "Microsoft.Authorization/elevateAccess/Action",
        ],
        data_actions: &[],
        not_data_actions: &[],
    },
    BuiltinRole {
        guid: "acdd72a7-3385-48ef-bd42-f606fba81ae7",
        role_name: "Reader",
        description: "View all resources, but does not allow you to make any changes.",
        actions: &["*/read"],
        not_actions: &[],
        data_actions: &[],
        not_data_actions: &[],
    },
    BuiltinRole {
        guid: "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9",
        role_name: "User Access Administrator",
        description: "Lets you manage user access to Azure resources.",
        actions: &["*/read", "Microsoft.Authorization/*", "Microsoft.Support/*"],
        not_actions: &[],
        data_actions: &[],
        not_data_actions: &[],
    },
    BuiltinRole {
        guid: "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
        role_name: "Storage Blob Data Contributor",
        description: "Allows for read, write and delete access to Azure Storage blob containers and data.",
        actions: &[
            "Microsoft.Storage/storageAccounts/blobServices/containers/delete",
            "Microsoft.Storage/storageAccounts/blobServices/containers/read",
            "Microsoft.Storage/storageAccounts/blobServices/containers/write",
            "Microsoft.Storage/storageAccounts/blobServices/generateUserDelegationKey/action",
        ],
        not_actions: &[],
        data_actions: &[
            "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/delete",
            "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read",
            "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write",
            "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/move/action",
            "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action",
        ],
        not_data_actions: &[],
    },
    BuiltinRole {
        guid: "4633458b-17de-408a-b874-0445c86b69e6",
        role_name: "Key Vault Secrets User",
        description: "Read secret contents. Only works for key vaults that use the 'Azure role-based access control' permission model.",
        actions: &["*/read"],
        not_actions: &[],
        data_actions: &[
            "Microsoft.KeyVault/vaults/secrets/getSecret/action",
            "Microsoft.KeyVault/vaults/secrets/readMetadata/action",
        ],
        not_data_actions: &[],
    },
    BuiltinRole {
        guid: "9980e02c-c2be-4d73-94e8-173b1dc7cf3c",
        role_name: "Virtual Machine Contributor",
        description: "Lets you manage virtual machines, but not access to them, and not the virtual network or storage account they are connected to.",
        actions: &[
            "Microsoft.Compute/availabilitySets/*",
            "Microsoft.Compute/locations/*",
            "Microsoft.Compute/virtualMachines/*",
            "Microsoft.Compute/virtualMachineScaleSets/*",
            "Microsoft.Network/networkInterfaces/read",
            "Microsoft.Storage/storageAccounts/read",
        ],
        not_actions: &[],
        data_actions: &[],
        not_data_actions: &[],
    },
    BuiltinRole {
        guid: "4d97b98b-1d4f-4787-a291-c67834d212e7",
        role_name: "Network Contributor",
        description: "Lets you manage networks, but not access to them.",
        actions: &[
            "Microsoft.Network/*",
            "Microsoft.Authorization/*/read",
            "Microsoft.Resources/deployments/*",
            "Microsoft.Resources/subscriptions/resourceGroups/read",
        ],
        not_actions: &[],
        data_actions: &[],
        not_data_actions: &[],
    },
];

// ---------------------------------------------------------------------------------
// Response DTOs — OWN Serialize shapes (verified 2022-04-01 camelCase casing).
// ---------------------------------------------------------------------------------

/// A `permissions[]` entry: `{actions, notActions, dataActions, notDataActions}`.
#[derive(Serialize)]
pub struct Permission {
    actions: Vec<String>,
    #[serde(rename = "notActions")]
    not_actions: Vec<String>,
    #[serde(rename = "dataActions")]
    data_actions: Vec<String>,
    #[serde(rename = "notDataActions")]
    not_data_actions: Vec<String>,
}

/// roleDefinition `properties`: `{roleName, type, description, assignableScopes, permissions}`.
#[derive(Serialize)]
pub struct RoleDefinitionProperties {
    #[serde(rename = "roleName")]
    role_name: String,
    #[serde(rename = "type")]
    role_type: String,
    description: String,
    #[serde(rename = "assignableScopes")]
    assignable_scopes: Vec<String>,
    permissions: Vec<Permission>,
}

/// A roleDefinition item: `{id, type, name, properties}`. The GET-at-scope `id` carries
/// the `/subscriptions/{sub}` prefix; `name` is the bare GUID.
#[derive(Serialize)]
pub struct RoleDefinition {
    id: String,
    #[serde(rename = "type")]
    resource_type: String,
    name: String,
    properties: RoleDefinitionProperties,
}

/// roleDefinitions list envelope `{value:[...]}`.
#[derive(Serialize)]
pub struct RoleDefinitionList {
    value: Vec<RoleDefinition>,
}

/// roleAssignment `properties`: `{principalId, principalType, roleDefinitionId, scope}`.
/// `roleDefinitionId` is TENANT-scoped (no `/subscriptions` prefix — RESEARCH Q3).
#[derive(Serialize)]
pub struct RoleAssignmentProperties {
    #[serde(rename = "principalId")]
    principal_id: String,
    #[serde(rename = "principalType")]
    principal_type: String,
    #[serde(rename = "roleDefinitionId")]
    role_definition_id: String,
    scope: String,
}

/// A roleAssignment item: `{name, type, id, properties}`.
#[derive(Serialize)]
pub struct RoleAssignment {
    name: String,
    #[serde(rename = "type")]
    resource_type: String,
    id: String,
    properties: RoleAssignmentProperties,
}

/// roleAssignments list envelope `{value:[...], nextLink}`. `nextLink` is serialized as
/// `null` for v2.0 (small assignment cardinalities).
#[derive(Serialize)]
pub struct RoleAssignmentList {
    value: Vec<RoleAssignment>,
    #[serde(rename = "nextLink")]
    next_link: Option<String>,
}

// ---------------------------------------------------------------------------------
// Catalogue helpers — case-insensitive GUID lookup + DTO mapping.
// ---------------------------------------------------------------------------------

/// Case-insensitive lookup of a built-in role by GUID (mirrors `cost.rs::service_name`'s
/// `eq_ignore_ascii_case` matching).
fn lookup_role(role_id: &str) -> Option<&'static BuiltinRole> {
    BUILTIN_ROLE_DEFINITIONS
        .iter()
        .find(|r| r.guid.eq_ignore_ascii_case(role_id))
}

/// Map a catalogue entry to the GET-at-scope [`RoleDefinition`] DTO (the `id` carries the
/// `/subscriptions/{sub}` prefix; the assignment reference drops it).
fn to_role_definition(role: &BuiltinRole, sub: &Uuid) -> RoleDefinition {
    RoleDefinition {
        id: format!(
            "/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions/{}",
            role.guid
        ),
        resource_type: "Microsoft.Authorization/roleDefinitions".to_string(),
        name: role.guid.to_string(),
        properties: RoleDefinitionProperties {
            role_name: role.role_name.to_string(),
            role_type: "BuiltInRole".to_string(),
            description: role.description.to_string(),
            assignable_scopes: vec!["/".to_string()],
            permissions: vec![Permission {
                actions: role.actions.iter().map(|s| s.to_string()).collect(),
                not_actions: role.not_actions.iter().map(|s| s.to_string()).collect(),
                data_actions: role.data_actions.iter().map(|s| s.to_string()).collect(),
                not_data_actions: role
                    .not_data_actions
                    .iter()
                    .map(|s| s.to_string())
                    .collect(),
            }],
        },
    }
}

// ---------------------------------------------------------------------------------
// roleAssignments `$filter` — a tiny dedicated parser for the two ARM forms this mock
// honors (`atScope()` and `principalId eq '{guid}'`) plus their `and`-composition. Any
// OTHER form (`assignedTo(...)`, `roleDefinitionId eq ...`, an unknown field/operator, a
// non-GUID principalId, or malformed input) is REJECTED with a fixed-string 400 — the
// endpoint never SILENTLY ignores a filter it cannot honor (the misleading behavior this
// closes). This is a SEPARATE grammar from the resource-list `$filter` (`filter.rs`),
// which keys on `resourceType`/`location`/tags — those fields have no meaning here.
// ---------------------------------------------------------------------------------
mod ra_filter {
    use crate::error::ApiError;
    use uuid::Uuid;

    /// The parsed roleAssignments `$filter`. A successful parse always sets at least one
    /// predicate (an empty filter is rejected), so it always contributes a `WHERE` conjunct.
    #[derive(Debug, Default, PartialEq, Eq)]
    pub(super) struct RaFilter {
        /// `atScope()` — Azure defines this as "assignments AT OR ABOVE the given scope"
        /// (i.e. this scope plus any inherited from parent management groups / the tenant
        /// root), which excludes the RG/resource-scoped assignments BELOW it. TenantLess
        /// models no management-group or tenant-root assignments, so at a subscription scope
        /// "at or above" reduces to EXACTLY `/subscriptions/{sub}` — [`to_conjunct`] filters
        /// on `scope = '/subscriptions/{sub}'`, which is the correct reduction of the Azure
        /// contract for the modeled subset, NOT the general "exactly this scope" rule.
        ///
        /// [`to_conjunct`]: RaFilter::to_conjunct
        pub at_scope: bool,
        /// `principalId eq '{guid}'` — restrict to one principal. The literal is validated
        /// as a GUID at parse time (a non-GUID is a 400), mirroring real ARM's GUID
        /// `principalId`; carrying a `Uuid` also means the bound value can never be a SQL
        /// metacharacter.
        pub principal_id: Option<Uuid>,
    }

    /// A lexical token: an identifier (`principalId`/`atScope`/`eq`/`and`), a single-quoted
    /// literal, or a paren (for the `atScope()` call form).
    #[derive(Debug, PartialEq, Eq)]
    enum Token {
        Ident(String),
        Literal(String),
        LParen,
        RParen,
    }

    /// The fixed-string 400 used for EVERY parse/validation failure — byte-identical to
    /// `filter.rs` so both `$filter` surfaces reject uniformly and leak nothing (T-04-04).
    fn bad() -> ApiError {
        ApiError::BadRequest {
            message: "invalid $filter".to_string(),
        }
    }

    /// Lex the filter into idents / single-quoted literals / parens. `''` inside a literal
    /// is one escaped quote (OData). Any other byte is a lex error → fixed 400.
    fn tokenize(input: &str) -> Result<Vec<Token>, ()> {
        let bytes = input.as_bytes();
        let mut tokens = Vec::new();
        let mut i = 0;
        while i < bytes.len() {
            let c = bytes[i];
            if c.is_ascii_whitespace() {
                i += 1;
            } else if c == b'(' {
                tokens.push(Token::LParen);
                i += 1;
            } else if c == b')' {
                tokens.push(Token::RParen);
                i += 1;
            } else if c == b'\'' {
                i += 1;
                let mut value = String::new();
                loop {
                    if i >= bytes.len() {
                        return Err(()); // unterminated literal (unbalanced quote)
                    }
                    if bytes[i] == b'\'' {
                        if i + 1 < bytes.len() && bytes[i + 1] == b'\'' {
                            value.push('\'');
                            i += 2;
                        } else {
                            i += 1; // closing quote
                            break;
                        }
                    } else {
                        let ch = input[i..].chars().next().ok_or(())?;
                        value.push(ch);
                        i += ch.len_utf8();
                    }
                }
                tokens.push(Token::Literal(value));
            } else if c.is_ascii_alphanumeric() {
                let start = i;
                while i < bytes.len() && bytes[i].is_ascii_alphanumeric() {
                    i += 1;
                }
                tokens.push(Token::Ident(input[start..i].to_string()));
            } else {
                return Err(()); // stray punctuation outside the grammar
            }
        }
        Ok(tokens)
    }

    /// Parse the supported roleAssignments `$filter` forms into a [`RaFilter`]. Returns the
    /// fixed-string 400 for any unsupported/malformed input — it NEVER falls through to a
    /// silent no-op. Never panics.
    ///
    /// The SAME byte/token execution budget the resource-list `$filter` enforces
    /// ([`crate::filter::MAX_FILTER_BYTES`] / [`crate::filter::MAX_FILTER_TOKENS`]) is
    /// applied here too — a long `atScope() and atScope() and …` chain is bounded at the
    /// source rather than being processed up to the HTTP request-line ceiling.
    pub(super) fn parse(input: &str) -> Result<RaFilter, ApiError> {
        // Byte cap BEFORE tokenizing (never build a huge token vec from an oversized value).
        crate::filter::check_filter_byte_budget(input)?;
        let tokens = tokenize(input).map_err(|_| bad())?;
        // Token cap AFTER tokenizing (bounds the number of clauses / conjuncts).
        crate::filter::check_filter_token_budget(tokens.len())?;
        let mut pos = 0;
        let mut out = RaFilter::default();

        loop {
            // ---- one clause: `atScope()` | `principalId eq '{guid}'` ----
            match tokens.get(pos) {
                Some(Token::Ident(kw)) if kw == "atScope" => {
                    pos += 1;
                    if tokens.get(pos) != Some(&Token::LParen)
                        || tokens.get(pos + 1) != Some(&Token::RParen)
                    {
                        return Err(bad());
                    }
                    pos += 2;
                    out.at_scope = true;
                }
                Some(Token::Ident(kw)) if kw == "principalId" => {
                    pos += 1;
                    match tokens.get(pos) {
                        Some(Token::Ident(op)) if op == "eq" => pos += 1,
                        _ => return Err(bad()), // missing/unknown operator
                    }
                    let lit = match tokens.get(pos) {
                        Some(Token::Literal(v)) => v,
                        _ => return Err(bad()), // missing literal
                    };
                    pos += 1;
                    let guid = Uuid::parse_str(lit).map_err(|_| bad())?; // GUID-only
                    if out.principal_id.is_some() {
                        return Err(bad()); // two principalId clauses in one filter
                    }
                    out.principal_id = Some(guid);
                }
                // empty, an unknown field, `assignedTo(...)`, `roleDefinitionId`, a bare
                // literal where a clause was expected — all rejected here.
                _ => return Err(bad()),
            }

            // ---- `and` continues the filter; anything else must be EOF ----
            match tokens.get(pos) {
                Some(Token::Ident(kw)) if kw == "and" => {
                    pos += 1;
                    continue;
                }
                None => break,
                _ => return Err(bad()), // trailing junk (e.g. two clauses without `and`)
            }
        }

        Ok(out)
    }

    impl RaFilter {
        /// Build the placeholders-only `WHERE` conjunct (`" AND (...)"`) for this filter,
        /// pushing each literal into `args` in bind order. `next` is the next free `$N`
        /// index, seeded PAST the handler's fixed binds; it advances as args are pushed so
        /// `#($N) == args.len()`.
        ///
        /// INVARIANT (the injection guard): the returned string carries ONLY column names,
        /// `$N` placeholders, a `::uuid` cast, parens, and `AND` — never a user literal. The
        /// principal GUID and the server-built scope BOTH flow through `args`.
        pub(super) fn to_conjunct(
            &self,
            sub: &Uuid,
            next: &mut i32,
            args: &mut Vec<String>,
        ) -> String {
            let mut parts = Vec::new();
            if self.at_scope {
                let idx = *next;
                *next += 1;
                args.push(format!("/subscriptions/{sub}"));
                parts.push(format!("scope = ${idx}"));
            }
            if let Some(pid) = self.principal_id {
                let idx = *next;
                *next += 1;
                args.push(pid.to_string());
                parts.push(format!("principal_oid = ${idx}::uuid"));
            }
            if parts.is_empty() {
                String::new()
            } else {
                format!(" AND ({})", parts.join(" AND "))
            }
        }
    }
}

// ---------------------------------------------------------------------------------
// Handlers — register INSIDE the `arm` router (any-Bearer + enforce swap inherited).
// ---------------------------------------------------------------------------------

/// `GET /subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions` — the
/// full static built-in catalogue mapped to the GET-at-scope id form, wrapped in
/// `{value:[...]}`. State is unused (the catalogue is a const), but the extractor keeps
/// the handler signature uniform with the rest of the `arm` router.
pub async fn list_role_definitions(
    State(_state): State<AppState>,
    Path(sub): Path<Uuid>,
) -> Result<Json<RoleDefinitionList>, ApiError> {
    let value = BUILTIN_ROLE_DEFINITIONS
        .iter()
        .map(|r| to_role_definition(r, &sub))
        .collect();
    Ok(Json(RoleDefinitionList { value }))
}

/// `GET /subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions/{roleId}`
/// — case-insensitive GUID lookup in the catalogue; 404 ResourceNotFound if absent.
/// Returns the bare roleDefinition object (NOT a `{value:[...]}` list).
pub async fn get_role_definition(
    State(_state): State<AppState>,
    Path((sub, role_id)): Path<(Uuid, String)>,
) -> Result<Json<RoleDefinition>, ApiError> {
    let role = lookup_role(&role_id).ok_or(ApiError::NotFound { what: role_id })?;
    Ok(Json(to_role_definition(role, &sub)))
}

/// `GET /subscriptions/{sub}/providers/Microsoft.Authorization/roleAssignments` — a
/// keyset-paginated sqlx read of `synthetic.role_assignments`, mapped into the verified
/// item shape. The stored `role_definition_id` is already tenant-scoped (writer contract),
/// so it is emitted verbatim.
///
/// Pagination mirrors the resource list (MOCK-03/08): keyset over the UUID PK
/// `assignment_id` with a `$top` clamp, an opaque `$skiptoken` continuation, and an
/// absolute `nextLink`. The subscription id and decoded cursor are `.bind()`-bound, never
/// spliced (project SQL bar / cost.rs precedent).
///
/// `$filter` (2022-04-01) is HONORED, not silently ignored: `atScope()` and
/// `principalId eq '{guid}'` (and their `and`-composition) map to a placeholders-only
/// `WHERE` conjunct via [`ra_filter`]; every other/malformed form short-circuits to a
/// fixed-string 400 via `?` BEFORE any SQL is built.
pub async fn list_role_assignments(
    State(state): State<AppState>,
    Path(sub): Path<Uuid>,
    Query(params): Query<PageParams>,
) -> Result<Json<RoleAssignmentList>, ApiError> {
    let top = clamp_top(params.top);
    let cursor = cursor_uuid_from_token(params.skiptoken.as_deref())?;

    // Parse `$filter` BEFORE building any SQL: an unsupported or malformed filter is an
    // explicit 400 (never a silent-ignore 200 — the misleading behavior this closes).
    // Fixed binds are $1 sub, $2 cursor, $3 top+1, so filter placeholders seed at $4.
    let parsed = params.filter.as_deref().map(ra_filter::parse).transpose()?;
    let mut next_param = 4;
    let mut filter_args = Vec::<String>::new();
    let where_extra = match &parsed {
        Some(f) => f.to_conjunct(&sub, &mut next_param, &mut filter_args),
        None => String::new(),
    };

    // `$3 = top + 1` fetches one surplus row to decide whether another page (and thus a
    // `nextLink`) is due — the same `LIMIT top+1` keyset trick the resource list uses.
    let sql = format!(
        "SELECT assignment_id, principal_oid, principal_type, role_definition_id, scope \
         FROM synthetic.role_assignments \
         WHERE subscription_id = $1 AND ($2::uuid IS NULL OR assignment_id > $2){where_extra} \
         ORDER BY assignment_id \
         LIMIT $3"
    );

    let mut q = sqlx::query(&sql).bind(sub).bind(cursor).bind(top + 1);
    // Dynamic bind loop — the SQL text contains only $N tokens; every literal is bound.
    for a in filter_args {
        q = q.bind(a);
    }
    let rows = q.fetch_all(&state.pool).await?;

    // `LIMIT top+1` split: a surplus row means another page exists (Pitfall 4).
    let has_more = rows.len() as i64 > top;
    let page = if has_more {
        &rows[..top as usize]
    } else {
        &rows[..]
    };

    let mut value = Vec::with_capacity(page.len());
    let mut last_assignment_id: Option<Uuid> = None;
    for row in page {
        let assignment_id: Uuid = row.try_get("assignment_id")?;
        let principal_oid: Uuid = row.try_get("principal_oid")?;
        let principal_type: String = row.try_get("principal_type")?;
        let role_definition_id: String = row.try_get("role_definition_id")?;
        let scope: String = row.try_get("scope")?;

        let name = assignment_id.to_string();
        // The roleAssignment `id` (2022-04-01) is rooted at the assignment's ACTUAL
        // scope — `{scope}/providers/Microsoft.Authorization/roleAssignments/{name}` —
        // NOT unconditionally at the subscription. For RG/resource-scoped assignments
        // the stored `scope` already carries the full
        // `/subscriptions/...[/resourceGroups/...[/providers/...]]` path, so the id must
        // mirror it (else `id` contradicts `properties.scope`).
        let id = format!("{scope}/providers/Microsoft.Authorization/roleAssignments/{name}");
        value.push(RoleAssignment {
            id,
            resource_type: "Microsoft.Authorization/roleAssignments".to_string(),
            name,
            properties: RoleAssignmentProperties {
                principal_id: principal_oid.to_string(),
                principal_type,
                role_definition_id,
                scope,
            },
        });
        last_assignment_id = Some(assignment_id);
    }

    // Emit an absolute `nextLink` (echoing `$top`/`api-version`/`$filter`) ONLY when a
    // surplus row proved another page exists — so a filtered/paged traversal replays the
    // same predicate on page 2+ (mirrors the resource list).
    let link = if has_more {
        last_assignment_id.map(|id| {
            let tok = encode_token(&id.to_string());
            let path =
                format!("/subscriptions/{sub}/providers/Microsoft.Authorization/roleAssignments");
            next_link(
                &state.base_url,
                &path,
                top,
                &tok,
                params.api_version.as_deref(),
                params.filter.as_deref(),
            )
        })
    } else {
        None
    };

    Ok(Json(RoleAssignmentList {
        value,
        next_link: link,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ra_filter::RaFilter;
    use serde_json::Value;

    // ---- roleAssignments `$filter` parser (DB-free) ------------------------

    /// `atScope()` parses to the at-scope-only filter.
    #[test]
    fn ra_filter_parses_at_scope() {
        assert_eq!(
            ra_filter::parse("atScope()").unwrap(),
            RaFilter {
                at_scope: true,
                principal_id: None,
            }
        );
    }

    /// `principalId eq '{guid}'` parses to the principal-only filter (GUID validated).
    #[test]
    fn ra_filter_parses_principal_id() {
        let g = Uuid::from_u128(0x0a0a_0a0a_0a0a_0a0a_0a0a_0a0a_0a0a_0a0a);
        assert_eq!(
            ra_filter::parse(&format!("principalId eq '{g}'")).unwrap(),
            RaFilter {
                at_scope: false,
                principal_id: Some(g),
            }
        );
    }

    /// The two clauses compose with `and`, in either order (AND is commutative).
    #[test]
    fn ra_filter_parses_combined_and() {
        let g = Uuid::from_u128(0x0c0c_0c0c_0c0c_0c0c_0c0c_0c0c_0c0c_0c0c);
        let expected = RaFilter {
            at_scope: true,
            principal_id: Some(g),
        };
        assert_eq!(
            ra_filter::parse(&format!("atScope() and principalId eq '{g}'")).unwrap(),
            expected
        );
        assert_eq!(
            ra_filter::parse(&format!("principalId eq '{g}' and atScope()")).unwrap(),
            expected
        );
    }

    /// Every unsupported or malformed form is the fixed-string 400 — NEVER a silent no-op.
    #[test]
    fn ra_filter_rejects_unsupported_and_malformed() {
        for bad in [
            "",
            "   ",
            "assignedTo('0a0a0a0a-0a0a-0a0a-0a0a-0a0a0a0a0a0a')", // unsupported form
            "roleDefinitionId eq '8e3af657-bb00-4899-acbc-f0f7f5db61aa'", // unsupported field
            "principalId eq 'not-a-guid'",                        // non-GUID literal
            "principalId eq",                                     // missing literal
            "principalId 'x'",                                    // missing operator
            "atScope(",                                           // unbalanced paren
            "atScope() and",                                      // trailing `and`
            "foo eq 'x'",                                         // unknown field
            "atScope() atScope()",                                // two clauses, no `and`
        ] {
            match ra_filter::parse(bad) {
                Err(ApiError::BadRequest { message }) => {
                    assert_eq!(message, "invalid $filter", "wrong message for {bad:?}");
                }
                other => panic!("expected a 400 for {bad:?}, got {other:?}"),
            }
        }
    }

    /// The roleAssignments `$filter` shares the resource-list byte budget: an over-byte
    /// filter (a syntactically-valid but enormous `atScope() and …` chain, so ONLY the size
    /// cap can reject it) is the fixed no-leak 400, rejected BEFORE tokenizing.
    #[test]
    fn ra_filter_rejects_over_byte_budget() {
        // `atScope() and ` is 14 bytes; ~200 of them clears the 2 KiB byte cap.
        let huge = format!("{}atScope()", "atScope() and ".repeat(200));
        assert!(huge.len() > crate::filter::MAX_FILTER_BYTES);
        match ra_filter::parse(&huge) {
            Err(ApiError::BadRequest { message }) => assert_eq!(message, "invalid $filter"),
            other => panic!("expected an over-byte 400, got {other:?}"),
        }
    }

    /// The roleAssignments `$filter` shares the resource-list token budget: a filter UNDER
    /// the byte cap but OVER the token cap is still the fixed no-leak 400. `atScope()` is 3
    /// tokens + `and` = 4 per clause, so ~60 clauses clears the 200-token cap while staying
    /// well under 2 KiB — proving the TOKEN axis, not the byte axis.
    #[test]
    fn ra_filter_rejects_over_token_budget() {
        let input = format!("atScope(){}", " and atScope()".repeat(60));
        assert!(
            input.len() <= crate::filter::MAX_FILTER_BYTES,
            "must exercise the TOKEN cap, not bytes"
        );
        match ra_filter::parse(&input) {
            Err(ApiError::BadRequest { message }) => assert_eq!(message, "invalid $filter"),
            other => panic!("expected an over-token 400, got {other:?}"),
        }
    }

    /// The conjunct is placeholders-only: a combined filter seeds at `$4` (past the
    /// handler's fixed `$1..$3`); the fragment carries ONLY columns, `$N`, a `::uuid`
    /// cast, and `AND` — the GUID + server-built scope flow through `args`.
    #[test]
    fn ra_filter_conjunct_is_placeholders_only() {
        let g = Uuid::from_u128(0x0c0c_0c0c_0c0c_0c0c_0c0c_0c0c_0c0c_0c0c);
        let sub = Uuid::from_u128(0x1111_1111_1111_1111_1111_1111_1111_1111);
        let f = ra_filter::parse(&format!("atScope() and principalId eq '{g}'")).unwrap();

        let mut next = 4;
        let mut args = Vec::new();
        let frag = f.to_conjunct(&sub, &mut next, &mut args);

        assert_eq!(frag, " AND (scope = $4 AND principal_oid = $5::uuid)");
        assert_eq!(args, vec![format!("/subscriptions/{sub}"), g.to_string()]);
        assert_eq!(next, 6, "two placeholders consumed");
        // Every `$N` has exactly one bound arg (no orphan placeholders).
        assert_eq!(frag.matches('$').count(), args.len());
        // The GUID literal never appears spliced in the fragment (data, not code).
        assert!(!frag.contains(&g.to_string()));
    }

    /// catalogue_is_constant: the static catalogue holds exactly the eight built-in
    /// GUID+roleName tuples; Owner/Contributor/Reader GUIDs are present; every entry is
    /// a BuiltInRole with `assignableScopes ["/"]` and a non-empty permissions vector.
    #[test]
    fn catalogue_is_constant() {
        assert_eq!(
            BUILTIN_ROLE_DEFINITIONS.len(),
            8,
            "the catalogue must hold exactly the eight built-in roles"
        );

        let by_guid = |g: &str| BUILTIN_ROLE_DEFINITIONS.iter().find(|r| r.guid == g);
        assert_eq!(
            by_guid("8e3af657-bb00-4899-acbc-f0f7f5db61aa").map(|r| r.role_name),
            Some("Owner")
        );
        assert_eq!(
            by_guid("b24988ac-6180-42a0-ab88-20f7382dd24c").map(|r| r.role_name),
            Some("Contributor")
        );
        assert_eq!(
            by_guid("acdd72a7-3385-48ef-bd42-f606fba81ae7").map(|r| r.role_name),
            Some("Reader")
        );

        for role in BUILTIN_ROLE_DEFINITIONS {
            let def = to_role_definition(role, &Uuid::nil());
            assert_eq!(
                def.properties.role_type, "BuiltInRole",
                "{}",
                role.role_name
            );
            assert_eq!(
                def.properties.assignable_scopes,
                vec!["/".to_string()],
                "{} assignableScopes must be [\"/\"]",
                role.role_name
            );
            assert!(
                !def.properties.permissions.is_empty(),
                "{} must carry a non-empty permissions vector",
                role.role_name
            );
        }
    }

    /// catalogue_pins_all_eight_guids (Pitfall 3 / WR-02): the FULL `(guid, roleName)`
    /// set served by the catalogue must match `identity.py BUILTIN_ROLE_DEFINITIONS`
    /// VERBATIM — order, GUIDs, AND roleNames. `catalogue_is_constant` only spot-checks
    /// Owner/Contributor/Reader (the fixture-seeded subset), so a typo in any of the five
    /// SPECIALIZED GUIDs (User Access Administrator, Storage Blob Data Contributor, Key
    /// Vault Secrets User, Virtual Machine Contributor, Network Contributor) on either side
    /// previously shipped silently. This checked-in list is the canonical source-of-truth
    /// copy from `src/tenantless/generator/identity.py`; any drift on the Rust side goes
    /// red here, and the Python side is pinned by this same list living in the docstring.
    #[test]
    fn catalogue_pins_all_eight_guids() {
        // Canonical (guid, roleName) tuples — byte-identical to identity.py.
        const EXPECTED: &[(&str, &str)] = &[
            ("8e3af657-bb00-4899-acbc-f0f7f5db61aa", "Owner"),
            ("b24988ac-6180-42a0-ab88-20f7382dd24c", "Contributor"),
            ("acdd72a7-3385-48ef-bd42-f606fba81ae7", "Reader"),
            (
                "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9",
                "User Access Administrator",
            ),
            (
                "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
                "Storage Blob Data Contributor",
            ),
            (
                "4633458b-17de-408a-b874-0445c86b69e6",
                "Key Vault Secrets User",
            ),
            (
                "9980e02c-c2be-4d73-94e8-173b1dc7cf3c",
                "Virtual Machine Contributor",
            ),
            (
                "4d97b98b-1d4f-4787-a291-c67834d212e7",
                "Network Contributor",
            ),
        ];

        let served: Vec<(&str, &str)> = BUILTIN_ROLE_DEFINITIONS
            .iter()
            .map(|r| (r.guid, r.role_name))
            .collect();

        assert_eq!(
            served.as_slice(),
            EXPECTED,
            "the served catalogue must match identity.py BUILTIN_ROLE_DEFINITIONS verbatim \
             (guid + roleName, in order) — a drift in ANY of the eight built-ins is a \
             cross-language coupling break (Pitfall 3 / WR-02)"
        );
    }

    /// role_definition_dto_shape: serializing a roleDefinition yields the verified field
    /// set with exact camelCase, and the GET-at-scope `id` carries the `/subscriptions`
    /// prefix.
    #[test]
    fn role_definition_dto_shape() {
        let owner = BUILTIN_ROLE_DEFINITIONS
            .iter()
            .find(|r| r.role_name == "Owner")
            .expect("Owner is in the catalogue");
        let sub = Uuid::from_u128(0x1111_1111_1111_1111_1111_1111_1111_1111);
        let v: Value = serde_json::to_value(to_role_definition(owner, &sub)).unwrap();

        assert_eq!(v["type"], "Microsoft.Authorization/roleDefinitions");
        assert_eq!(v["name"], owner.guid);
        assert_eq!(
            v["id"],
            format!(
                "/subscriptions/{sub}/providers/Microsoft.Authorization/roleDefinitions/{}",
                owner.guid
            )
        );

        let p = &v["properties"];
        assert_eq!(p["roleName"], "Owner");
        assert_eq!(p["type"], "BuiltInRole");
        assert!(p["description"].is_string(), "description must be a string");
        assert_eq!(p["assignableScopes"], serde_json::json!(["/"]));
        let perm = &p["permissions"][0];
        for key in ["actions", "notActions", "dataActions", "notDataActions"] {
            assert!(perm.get(key).is_some(), "permissions[0] missing {key}");
            assert!(
                perm[key].is_array(),
                "permissions[0].{key} must be an array"
            );
        }
    }

    /// role_assignment_dto_shape: serializing a roleAssignment yields
    /// `{name,type,id,properties{principalId,principalType,roleDefinitionId,scope}}` with
    /// the TENANT-scoped roleDefinitionId (no `/subscriptions` prefix).
    #[test]
    fn role_assignment_dto_shape() {
        let sub = Uuid::from_u128(0x1111_1111_1111_1111_1111_1111_1111_1111);
        let assignment_id = Uuid::from_u128(0xabcd_abcd_abcd_abcd_abcd_abcd_abcd_abcd);
        let principal = Uuid::from_u128(0x2222_2222_2222_2222_2222_2222_2222_2222);
        let role_definition_id =
            format!("{ROLE_DEF_ID_PREFIX}8e3af657-bb00-4899-acbc-f0f7f5db61aa");

        let ra = RoleAssignment {
            name: assignment_id.to_string(),
            resource_type: "Microsoft.Authorization/roleAssignments".to_string(),
            id: format!(
                "/subscriptions/{sub}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}"
            ),
            properties: RoleAssignmentProperties {
                principal_id: principal.to_string(),
                principal_type: "ServicePrincipal".to_string(),
                role_definition_id: role_definition_id.clone(),
                scope: format!("/subscriptions/{sub}"),
            },
        };
        let v: Value = serde_json::to_value(&ra).unwrap();

        assert_eq!(v["type"], "Microsoft.Authorization/roleAssignments");
        assert!(v["name"].is_string());
        assert!(
            v["id"]
                .as_str()
                .unwrap()
                .contains("/providers/Microsoft.Authorization/roleAssignments/")
        );

        let p = &v["properties"];
        for key in ["principalId", "principalType", "roleDefinitionId", "scope"] {
            assert!(p.get(key).is_some(), "properties missing {key}");
        }
        let rdid = p["roleDefinitionId"].as_str().unwrap();
        assert!(
            rdid.starts_with("/providers/Microsoft.Authorization/roleDefinitions/"),
            "roleDefinitionId must be tenant-scoped: {rdid}"
        );
        assert!(
            !rdid.contains("/subscriptions/"),
            "roleDefinitionId must NOT carry a /subscriptions prefix: {rdid}"
        );
    }
}
