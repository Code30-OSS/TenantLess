//! HTTP handler modules. Wave 1 registers only `/subscriptions`; Wave 2 adds the
//! resourceGroups/resources routes.

pub mod authorization;
pub mod cost;
pub mod drift;
pub mod resource_detail;
pub mod resource_groups;
pub mod resources;
pub mod sim;
pub mod subscriptions;
pub mod token;

pub use authorization::{get_role_definition, list_role_assignments, list_role_definitions};
pub use cost::{cost_query, cost_query_scoped};
pub use drift::{by_resource, get_batch, list_drift};
pub use resource_detail::get_resource_detail;
pub use resource_groups::list_resource_groups;
pub use resources::{list_resources, list_rg_resources};
pub use sim::{list_dependencies, list_violations, summary};
pub use subscriptions::list_subscriptions;
