//! ARM response DTOs and the FromRow read structs they project from.
//!
//! The envelope is `{ "value": [...], "nextLink": ... }` with `nextLink` omitted
//! on the last page (MOCK-08). DTO field names follow ARM camelCase via
//! `#[serde(rename_all = "camelCase")]`. `subscriptions.id` is NOT stored — it is
//! synthesized as `/subscriptions/{subscription_id}` (Pitfall 5). The column sets
//! mirror the `writer.py` COPY contracts exactly (no `SELECT *`).

use serde::Serialize;
use serde_json::{Value, json};
use uuid::Uuid;

/// Generic ARM list envelope. `next_link` serializes as `nextLink` and is omitted
/// when absent (final page).
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ListResponse<T> {
    pub value: Vec<T>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub next_link: Option<String>,
}

impl<T> ListResponse<T> {
    /// A list page with no continuation (no `nextLink`).
    pub fn new(value: Vec<T>) -> Self {
        ListResponse {
            value,
            next_link: None,
        }
    }
}

/// Row projected from `synthetic.subscriptions` (writer.py copy_subscriptions
/// contract; archetype/tags unused here). `subscription_id`/`tenant_id` are UUID
/// columns.
#[derive(sqlx::FromRow)]
pub struct SubscriptionRow {
    pub subscription_id: Uuid,
    pub tenant_id: Uuid,
    pub display_name: String,
    pub state: String,
    pub authorization_source: String,
    pub spending_limit: String,
}

/// The minimal `subscriptionPolicies` object (A3): just `spendingLimit`.
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SubscriptionPolicies {
    pub spending_limit: String,
}

/// ARM subscription envelope item (MOCK-01).
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Subscription {
    /// Synthesized: `/subscriptions/{subscription_id}`.
    pub id: String,
    pub subscription_id: String,
    pub display_name: String,
    pub state: String,
    pub tenant_id: String,
    pub authorization_source: String,
    /// Always an object, never null (MOCK-13 shape discipline at the envelope level).
    pub subscription_policies: SubscriptionPolicies,
}

impl From<SubscriptionRow> for Subscription {
    fn from(row: SubscriptionRow) -> Self {
        let subscription_id = row.subscription_id.to_string();
        Subscription {
            id: format!("/subscriptions/{subscription_id}"),
            subscription_id,
            display_name: row.display_name,
            state: row.state,
            tenant_id: row.tenant_id.to_string(),
            authorization_source: row.authorization_source,
            subscription_policies: SubscriptionPolicies {
                spending_limit: row.spending_limit,
            },
        }
    }
}

/// Row projected from `synthetic.resource_groups` (writer.py copy_resource_groups
/// contract). `id` is already a fully-formed ARM path (PK TEXT), `tags` is JSONB.
/// The table has NO `properties` column — `provisioning_state` is synthesized into
/// the ARM `properties` object below.
#[derive(sqlx::FromRow)]
pub struct ResourceGroupRow {
    pub id: String,
    pub name: String,
    pub location: String,
    pub tags: sqlx::types::Json<Value>,
    pub provisioning_state: String,
}

/// ARM resource-group list item (MOCK-02). `id` is echoed verbatim (already ARM),
/// `type` is the const `Microsoft.Resources/resourceGroups`, and `properties` is
/// always an object carrying `provisioningState` (MOCK-13).
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ResourceGroup {
    pub id: String,
    pub name: String,
    pub r#type: String,
    pub location: String,
    pub tags: Value,
    /// Always an object (synthesized): `{ "provisioningState": <state> }`.
    pub properties: Value,
}

impl From<ResourceGroupRow> for ResourceGroup {
    fn from(row: ResourceGroupRow) -> Self {
        ResourceGroup {
            id: row.id,
            name: row.name,
            r#type: "Microsoft.Resources/resourceGroups".to_string(),
            location: row.location,
            tags: row.tags.0,
            properties: json!({ "provisioningState": row.provisioning_state }),
        }
    }
}

/// Row projected from `synthetic.resources` (writer.py copy_resources contract).
/// `r#type` is the raw identifier for the `type` column; `sku`/`kind` are nullable;
/// `properties` is JSONB NOT NULL (defaults to `'{}'`).
#[derive(sqlx::FromRow)]
pub struct ResourceRow {
    pub id: String,
    pub name: String,
    pub r#type: String,
    pub location: String,
    pub tags: sqlx::types::Json<Value>,
    pub sku: Option<sqlx::types::Json<Value>>,
    pub kind: Option<String>,
    pub properties: sqlx::types::Json<Value>,
}

/// ARM resource list item (MOCK-03). `id`/`type` echoed verbatim (no casing logic —
/// MOCK-12 is Phase 4); `sku`/`kind` omitted when NULL; `properties` always an
/// object — a defensive `Null` coalesces to `{}` (MOCK-13).
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Resource {
    pub id: String,
    pub name: String,
    pub r#type: String,
    pub location: String,
    pub tags: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sku: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,
    /// Always an object; a `null` JSONB value is coalesced to `{}` (MOCK-13).
    pub properties: Value,
}

impl From<ResourceRow> for Resource {
    fn from(row: ResourceRow) -> Self {
        let properties = match row.properties.0 {
            Value::Null => json!({}),
            other => other,
        };
        Resource {
            id: row.id,
            name: row.name,
            // MOCK-12: ARM responses use canonical type casing even when the stored
            // value is the half-canonical real-profile form (`Microsoft.storage/...`).
            r#type: crate::casing::canonical_type(&row.r#type),
            location: row.location,
            tags: row.tags.0,
            sku: row.sku.map(|s| s.0),
            kind: row.kind,
            properties,
        }
    }
}
