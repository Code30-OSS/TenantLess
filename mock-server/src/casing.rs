//! Canonical Azure resource-type casing (MOCK-12).
//!
//! The mock owns MOCK-12 ("type field uses canonical casing, e.g.
//! `Microsoft.Storage/storageAccounts`, not `microsoft.storage/storageaccounts`").
//! Upstream, the analyzer's `normalize_type_key` (01.1) only canonicalizes the leading
//! `microsoft.` namespace token and PRESERVES the rest lowercase (a lossless-determinism
//! choice), and the generator stores that verbatim — so a real-profile-derived tenant
//! holds types like `Microsoft.storage/storageaccounts`. Azure's canonical casing is NOT
//! algorithmically derivable from a lowercase source (`storageaccounts` -> `storageAccounts`,
//! `virtualmachines` -> `virtualMachines` require knowing each type), so we map the
//! known/governance-relevant types explicitly here and pass unknown types through
//! unchanged (best-effort). This table is the single extension point for new types.

/// Canonicalize a resource `type` string for ARM output (MOCK-12).
///
/// Matches case-insensitively on the full `namespace/type` string and returns the
/// canonical Azure form for known types; any unmapped type is returned unchanged.
pub fn canonical_type(raw: &str) -> String {
    let canon: &str = match raw.to_ascii_lowercase().as_str() {
        // Storage
        "microsoft.storage/storageaccounts" => "Microsoft.Storage/storageAccounts",
        // Network
        "microsoft.network/virtualnetworks" => "Microsoft.Network/virtualNetworks",
        "microsoft.network/networkinterfaces" => "Microsoft.Network/networkInterfaces",
        "microsoft.network/networksecuritygroups" => "Microsoft.Network/networkSecurityGroups",
        "microsoft.network/publicipaddresses" => "Microsoft.Network/publicIPAddresses",
        "microsoft.network/privateendpoints" => "Microsoft.Network/privateEndpoints",
        "microsoft.network/loadbalancers" => "Microsoft.Network/loadBalancers",
        "microsoft.network/virtualnetworkgateways" => "Microsoft.Network/virtualNetworkGateways",
        "microsoft.network/routetables" => "Microsoft.Network/routeTables",
        "microsoft.network/privatednszones" => "Microsoft.Network/privateDnsZones",
        "microsoft.network/applicationgateways" => "Microsoft.Network/applicationGateways",
        // Compute
        "microsoft.compute/virtualmachines" => "Microsoft.Compute/virtualMachines",
        "microsoft.compute/disks" => "Microsoft.Compute/disks",
        "microsoft.compute/virtualmachinescalesets" => "Microsoft.Compute/virtualMachineScaleSets",
        "microsoft.compute/availabilitysets" => "Microsoft.Compute/availabilitySets",
        "microsoft.compute/images" => "Microsoft.Compute/images",
        "microsoft.compute/snapshots" => "Microsoft.Compute/snapshots",
        // Key Vault
        "microsoft.keyvault/vaults" => "Microsoft.KeyVault/vaults",
        // SQL (incl. the nested servers/databases form)
        "microsoft.sql/servers" => "Microsoft.Sql/servers",
        "microsoft.sql/servers/databases" => "Microsoft.Sql/servers/databases",
        "microsoft.dbforpostgresql/servers" => "Microsoft.DBforPostgreSQL/servers",
        "microsoft.dbforpostgresql/flexibleservers" => "Microsoft.DBforPostgreSQL/flexibleServers",
        "microsoft.dbformysql/servers" => "Microsoft.DBforMySQL/servers",
        "microsoft.dbformysql/flexibleservers" => "Microsoft.DBforMySQL/flexibleServers",
        // Containers
        "microsoft.containerservice/managedclusters" => {
            "Microsoft.ContainerService/managedClusters"
        }
        "microsoft.containerregistry/registries" => "Microsoft.ContainerRegistry/registries",
        // Web / App
        "microsoft.web/sites" => "Microsoft.Web/sites",
        "microsoft.web/serverfarms" => "Microsoft.Web/serverfarms",
        "microsoft.web/staticsites" => "Microsoft.Web/staticSites",
        "microsoft.app/containerapps" => "Microsoft.App/containerApps",
        "microsoft.app/managedenvironments" => "Microsoft.App/managedEnvironments",
        // Observability
        "microsoft.insights/components" => "Microsoft.Insights/components",
        "microsoft.operationalinsights/workspaces" => "Microsoft.OperationalInsights/workspaces",
        // Messaging
        "microsoft.eventhub/namespaces" => "Microsoft.EventHub/namespaces",
        "microsoft.servicebus/namespaces" => "Microsoft.ServiceBus/namespaces",
        "microsoft.eventgrid/topics" => "Microsoft.EventGrid/topics",
        // Data / misc
        "microsoft.documentdb/databaseaccounts" => "Microsoft.DocumentDB/databaseAccounts",
        "microsoft.cache/redis" => "Microsoft.Cache/Redis",
        "microsoft.apimanagement/service" => "Microsoft.ApiManagement/service",
        "microsoft.appconfiguration/configurationstores" => {
            "Microsoft.AppConfiguration/configurationStores"
        }
        "microsoft.managedidentity/userassignedidentities" => {
            "Microsoft.ManagedIdentity/userAssignedIdentities"
        }
        "microsoft.recoveryservices/vaults" => "Microsoft.RecoveryServices/vaults",
        "microsoft.cognitiveservices/accounts" => "Microsoft.CognitiveServices/accounts",
        "microsoft.datafactory/factories" => "Microsoft.DataFactory/factories",
        // Unknown / long-tail: return the stored value unchanged (best-effort).
        _ => return raw.to_string(),
    };
    canon.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonicalizes_lowercased_storage_type() {
        // The real-profile shape (leading token canonicalized upstream, rest lowercase).
        assert_eq!(
            canonical_type("Microsoft.storage/storageaccounts"),
            "Microsoft.Storage/storageAccounts"
        );
    }

    #[test]
    fn canonicalizes_fully_lowercase_input() {
        // Even an all-lowercase scan string maps to canonical.
        assert_eq!(
            canonical_type("microsoft.network/networkinterfaces"),
            "Microsoft.Network/networkInterfaces"
        );
    }

    #[test]
    fn already_canonical_is_idempotent() {
        // canonicalize(canonical) == canonical for mapped types (fixtures rely on this).
        for t in [
            "Microsoft.Storage/storageAccounts",
            "Microsoft.Network/virtualNetworks",
            "Microsoft.Sql/servers/databases",
        ] {
            assert_eq!(canonical_type(t), t, "{t} must be idempotent");
        }
    }

    #[test]
    fn unknown_type_passes_through_unchanged() {
        // Long-tail types we don't map are returned verbatim (no corruption).
        assert_eq!(
            canonical_type("Microsoft.somethingnew/weirdthings"),
            "Microsoft.somethingnew/weirdthings"
        );
    }
}
