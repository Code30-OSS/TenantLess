#!/usr/bin/env python3
"""Build ``profiles/oss-bootstrap.json`` -- the hand-authored seed profile.

WHY THIS FILE EXISTS
====================
The bundled ``enterprise`` profile must be provably synthetic end to end. This
script is step 1 of that chain:

    build_oss_bootstrap_profile.py   ->  profiles/oss-bootstrap.json   (hand-authored)
    tenantless generate --profile .. ->  a synthetic estate in Postgres
    export_estate_duckdb.py          ->  a DuckDB view of that estate
    tenantless analyze duckdb:..     ->  src/tenantless/profiles/enterprise.json

Every number below was written by hand from PUBLIC Azure concepts -- Cloud
Adoption Framework landing-zone archetypes, public ARM resource-type names,
public region names, and conventional Azure tagging practice. Nothing here was
measured from, fitted to, or copied out of any real tenant. That is the whole
point: the profile that ships publicly has no upstream except this file, and
this file has no upstream except public documentation and the author's judgment.

The resulting estate is *plausible*, not *calibrated*. It is deliberately shaped
so the downstream analyzer sees enough structure to fit a full profile -- several
subscription archetypes with different sizes, resource groups whose type
signatures match the archetype catalog in ``generator/archetypes.py`` (so
semantic RG naming is genuinely exercised), a realistic long tail of resource
types, and cost spreads wide enough to fit lognormals against.

Run:
    uv run python scripts/build_oss_bootstrap_profile.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "profiles" / "oss-bootstrap.json"

# --------------------------------------------------------------------------
# Scale. Round, obviously-authored numbers -- deliberately NOT the shape of any
# real estate, and deliberately not a near-miss of one either.
# --------------------------------------------------------------------------
TOTAL_SUBSCRIPTIONS = 250
TOTAL_RESOURCE_GROUPS = 5_000
TOTAL_RESOURCES = 60_000

# --------------------------------------------------------------------------
# Public Azure regions. Weighting is an authored "EU-primary, US-secondary"
# footprint -- a common enterprise shape, not a measured one.
# --------------------------------------------------------------------------
EU_HEAVY = {
    "westeurope": 0.42,
    "northeurope": 0.22,
    "francecentral": 0.12,
    "uksouth": 0.09,
    "eastus": 0.07,
    "eastus2": 0.04,
    "westus2": 0.02,
    "southeastasia": 0.02,
}
EU_BALANCED = {
    "westeurope": 0.34,
    "northeurope": 0.20,
    "francecentral": 0.14,
    "uksouth": 0.12,
    "eastus": 0.10,
    "eastus2": 0.06,
    "southeastasia": 0.04,
}
DEV_SPREAD = {
    "westeurope": 0.46,
    "northeurope": 0.24,
    "uksouth": 0.14,
    "francecentral": 0.10,
    "eastus": 0.06,
}


def _dist(mean: float, std: float, lo: float, hi: float) -> dict[str, float]:
    return {"mean": mean, "std": std, "min": lo, "max": hi}


def _dist_nm(mean: float, std: float) -> dict[str, float]:
    return {"mean": mean, "std": std}


# --------------------------------------------------------------------------
# Subscription archetypes -- Azure Cloud Adoption Framework landing zones.
# Platform subscriptions are few but dense; workload subscriptions are many.
# --------------------------------------------------------------------------
SUBSCRIPTION_ARCHETYPES = [
    {
        "id": "platform-connectivity",
        "weight": 0.04,
        "resource_group_count": _dist(12.0, 4.0, 3.0, 30.0),
        "resource_count": _dist(420.0, 170.0, 40.0, 1200.0),
        "location_distribution": EU_HEAVY,
        "tag_density": _dist_nm(5.0, 1.2),
    },
    {
        "id": "platform-management",
        "weight": 0.06,
        "resource_group_count": _dist(14.0, 5.0, 3.0, 36.0),
        "resource_count": _dist(300.0, 140.0, 30.0, 900.0),
        "location_distribution": EU_HEAVY,
        "tag_density": _dist_nm(5.4, 1.2),
    },
    {
        "id": "corp-production",
        "weight": 0.30,
        "resource_group_count": _dist(26.0, 12.0, 4.0, 90.0),
        "resource_count": _dist(430.0, 260.0, 20.0, 2400.0),
        "location_distribution": EU_BALANCED,
        "tag_density": _dist_nm(6.0, 1.5),
    },
    {
        "id": "corp-nonproduction",
        "weight": 0.38,
        "resource_group_count": _dist(18.0, 9.0, 2.0, 60.0),
        "resource_count": _dist(180.0, 120.0, 5.0, 900.0),
        "location_distribution": DEV_SPREAD,
        "tag_density": _dist_nm(4.4, 1.6),
    },
    {
        "id": "sandbox",
        "weight": 0.22,
        "resource_group_count": _dist(6.0, 4.0, 1.0, 24.0),
        "resource_count": _dist(45.0, 40.0, 1.0, 260.0),
        "location_distribution": DEV_SPREAD,
        "tag_density": _dist_nm(2.6, 1.4),
    },
]

# --------------------------------------------------------------------------
# Resource-group templates.
#
# The type_sets are authored to match the archetype signatures in
# ``generator/archetypes.py`` so the generated estate genuinely exercises
# semantic RG naming (anchor types present, negative signals absent). An
# archetype whose anchor never appears in any template would make the naming
# gate vacuous -- see the non-vacuity floors in the Phase 19 audit.
# --------------------------------------------------------------------------
RG_TEMPLATES = [
    {
        "id": "vm-workload",
        "weight": 0.20,
        "type_set": [
            "Microsoft.Compute/virtualMachines",
            "Microsoft.Compute/disks",
            "Microsoft.Network/networkInterfaces",
            "Microsoft.Compute/availabilitySets",
            "Microsoft.Network/publicIPAddresses",
        ],
        "resource_count": _dist(11.0, 6.0, 3.0, 60.0),
    },
    {
        "id": "web-app",
        "weight": 0.14,
        "type_set": [
            "Microsoft.Web/sites",
            "Microsoft.Web/serverfarms",
            "Microsoft.Insights/components",
            "Microsoft.Storage/storageAccounts",
        ],
        "resource_count": _dist(7.0, 4.0, 2.0, 34.0),
    },
    {
        "id": "network-hub",
        "weight": 0.09,
        "type_set": [
            "Microsoft.Network/virtualNetworks",
            "Microsoft.Network/networkSecurityGroups",
            "Microsoft.Network/routeTables",
            "Microsoft.Network/publicIPAddresses",
            "Microsoft.Network/azureFirewalls",
        ],
        "resource_count": _dist(9.0, 5.0, 2.0, 44.0),
    },
    {
        "id": "sql-database",
        "weight": 0.09,
        "type_set": [
            "Microsoft.Sql/servers",
            "Microsoft.Sql/servers/databases",
            "Microsoft.KeyVault/vaults",
        ],
        "resource_count": _dist(6.0, 3.0, 2.0, 28.0),
    },
    {
        "id": "aks-platform",
        "weight": 0.07,
        "type_set": [
            "Microsoft.ContainerService/managedClusters",
            "Microsoft.ContainerRegistry/registries",
            "Microsoft.ManagedIdentity/userAssignedIdentities",
        ],
        "resource_count": _dist(5.0, 2.0, 2.0, 18.0),
    },
    {
        "id": "data-platform",
        "weight": 0.06,
        "type_set": [
            "Microsoft.DataFactory/factories",
            "Microsoft.Databricks/workspaces",
            "Microsoft.Storage/storageAccounts",
            "Microsoft.KeyVault/vaults",
        ],
        "resource_count": _dist(8.0, 4.0, 2.0, 36.0),
    },
    {
        "id": "monitoring",
        "weight": 0.06,
        "type_set": [
            "Microsoft.OperationalInsights/workspaces",
            "Microsoft.Insights/actionGroups",
            "Microsoft.Insights/scheduledQueryRules",
            "Microsoft.Insights/activityLogAlerts",
        ],
        "resource_count": _dist(6.0, 3.0, 2.0, 26.0),
    },
    {
        "id": "backup",
        "weight": 0.05,
        "type_set": [
            "Microsoft.RecoveryServices/vaults",
            "Microsoft.Maintenance/maintenanceConfigurations",
            "Microsoft.Storage/storageAccounts",
        ],
        "resource_count": _dist(4.0, 2.0, 1.0, 16.0),
    },
    {
        "id": "messaging",
        "weight": 0.05,
        "type_set": [
            "Microsoft.ServiceBus/namespaces",
            "Microsoft.EventHub/namespaces",
            "Microsoft.EventGrid/topics",
            "Microsoft.Storage/storageAccounts",
        ],
        "resource_count": _dist(5.0, 3.0, 1.0, 22.0),
    },
    {
        "id": "identity",
        "weight": 0.04,
        "type_set": ["Microsoft.ManagedIdentity/userAssignedIdentities"],
        "resource_count": _dist(4.0, 3.0, 1.0, 20.0),
    },
    {
        "id": "devbox-platform",
        "weight": 0.02,
        "type_set": [
            "Microsoft.DevCenter/devcenters",
            "Microsoft.DevCenter/projects",
            "Microsoft.KeyVault/vaults",
        ],
        "resource_count": _dist(4.0, 2.0, 2.0, 14.0),
    },
    # The folded bucket. Real analyzed profiles always carry one (it is where
    # min-aggregation puts RGs whose exact type signature is too rare to keep),
    # so the bootstrap carries one too -- otherwise the generated estate would
    # have an unnaturally clean template distribution and the re-analyzed
    # profile would not exercise the __misc__ code path at all.
    {
        "id": "__misc__",
        "weight": 0.13,
        "type_set": ["__misc__"],
        "empty_share": 0.22,
        "type_weights": {
            "Microsoft.Storage/storageAccounts": 0.17,
            "Microsoft.Network/networkSecurityGroups": 0.11,
            "Microsoft.KeyVault/vaults": 0.10,
            "Microsoft.Compute/disks": 0.09,
            "Microsoft.Network/privateEndpoints": 0.08,
            "Microsoft.ManagedIdentity/userAssignedIdentities": 0.07,
            "Microsoft.Insights/components": 0.07,
            "Microsoft.Network/publicIPAddresses": 0.06,
            "Microsoft.Web/sites": 0.05,
            "Microsoft.Compute/virtualMachines": 0.05,
            "Microsoft.Network/privateDnsZones": 0.04,
            "Microsoft.Network/networkInterfaces": 0.04,
            "Microsoft.Automation/automationAccounts": 0.03,
            "Microsoft.Logic/workflows": 0.02,
            "Microsoft.ApiManagement/service": 0.02,
        },
        "resource_count": _dist(4.0, 4.0, 1.0, 40.0),
    },
]

# --------------------------------------------------------------------------
# Resource-type distributions.
#
# Frequencies are an authored long-tail: networking + compute primitives
# dominate by count (every VM drags a NIC and one or more disks), platform
# services sit in the middle, and specialist services form the tail. Property
# histograms use ONLY public ARM field names and public enum values.
# --------------------------------------------------------------------------
TYPES: dict[str, dict] = {
    "Microsoft.Compute/virtualMachines": {
        "frequency": 0.088,
        "property_distributions": {
            "vmSize": {
                "Standard_D2s_v3": 0.22,
                "Standard_D4s_v3": 0.18,
                "Standard_D8s_v3": 0.10,
                "Standard_B2ms": 0.14,
                "Standard_E4s_v3": 0.09,
                "Standard_E8s_v3": 0.05,
                "Standard_F4s_v2": 0.07,
                "Standard_D16s_v3": 0.04,
                "__other__": 0.11,
            },
            "osType": {"Linux": 0.62, "Windows": 0.38},
            "provisioningState": {"Succeeded": 0.97, "Failed": 0.02, "Updating": 0.01},
        },
    },
    "Microsoft.Network/networkInterfaces": {
        "frequency": 0.084,
        "property_distributions": {
            "enableAcceleratedNetworking": {"false": 0.71, "true": 0.29},
            "enableIPForwarding": {"false": 0.94, "true": 0.06},
            "nicType": {"Standard": 0.98, "Elastic": 0.02},
        },
    },
    "Microsoft.Compute/disks": {
        "frequency": 0.096,
        "property_distributions": {
            "diskSizeGB": {
                "32": 0.10,
                "64": 0.15,
                "128": 0.28,
                "256": 0.20,
                "512": 0.13,
                "1024": 0.09,
                "2048": 0.05,
            },
            "osType": {"Linux": 0.55, "Windows": 0.33, "null": 0.12},
        },
        "sku_distributions": {
            "name": {"Premium_LRS": 0.44, "StandardSSD_LRS": 0.36, "Standard_LRS": 0.20}
        },
    },
    "Microsoft.Storage/storageAccounts": {
        "frequency": 0.082,
        "property_distributions": {
            "accessTier": {"Hot": 0.74, "Cool": 0.26},
            "supportsHttpsTrafficOnly": {"true": 0.93, "false": 0.07},
            "minimumTlsVersion": {"TLS1_2": 0.86, "TLS1_0": 0.09, "TLS1_1": 0.05},
            "allowBlobPublicAccess": {"false": 0.88, "true": 0.12},
        },
        "sku_distributions": {
            "name": {
                "Standard_LRS": 0.52,
                "Standard_GRS": 0.24,
                "Standard_ZRS": 0.14,
                "Premium_LRS": 0.10,
            }
        },
        "kind_distributions": {"StorageV2": 0.91, "BlobStorage": 0.06, "Storage": 0.03},
    },
    "Microsoft.Network/networkSecurityGroups": {
        "frequency": 0.055,
        "property_distributions": {
            "defaultSecurityRulesOnly": {"false": 0.82, "true": 0.18},
            "securityRuleCount": {"2": 0.18, "4": 0.26, "6": 0.22, "9": 0.19, "14": 0.15},
        },
    },
    "Microsoft.Network/publicIPAddresses": {
        "frequency": 0.049,
        "property_distributions": {
            "publicIPAllocationMethod": {"Static": 0.68, "Dynamic": 0.32},
            "publicIPAddressVersion": {"IPv4": 0.97, "IPv6": 0.03},
        },
        "sku_distributions": {"name": {"Standard": 0.74, "Basic": 0.26}},
    },
    "Microsoft.KeyVault/vaults": {
        "frequency": 0.046,
        "property_distributions": {
            "enableSoftDelete": {"true": 0.89, "false": 0.11},
            "enablePurgeProtection": {"true": 0.61, "false": 0.39},
            "enableRbacAuthorization": {"true": 0.58, "false": 0.42},
        },
    },
    "Microsoft.Web/sites": {
        "frequency": 0.044,
        "property_distributions": {
            "state": {"Running": 0.91, "Stopped": 0.09},
            "httpsOnly": {"true": 0.84, "false": 0.16},
            "kind": {"app": 0.44, "app,linux": 0.31, "functionapp": 0.19, "app,container": 0.06},
        },
    },
    "Microsoft.Network/virtualNetworks": {
        "frequency": 0.036,
        "property_distributions": {
            "enableDdosProtection": {"false": 0.90, "true": 0.10},
            "subnetCount": {"1": 0.20, "2": 0.24, "3": 0.21, "5": 0.19, "8": 0.16},
            "addressSpacePrefixCount": {"1": 0.83, "2": 0.13, "3": 0.04},
        },
    },
    "Microsoft.Web/serverfarms": {
        "frequency": 0.032,
        "property_distributions": {
            "reserved": {"true": 0.46, "false": 0.54},
            "kind": {"app": 0.55, "linux": 0.34, "functionapp": 0.11},
        },
        "sku_distributions": {
            "name": {"P1v3": 0.24, "P2v3": 0.14, "S1": 0.22, "B1": 0.18, "Y1": 0.12, "EP1": 0.10}
        },
    },
    "Microsoft.Sql/servers/databases": {
        "frequency": 0.031,
        "property_distributions": {
            "status": {"Online": 0.95, "Paused": 0.05},
            "collation": {"SQL_Latin1_General_CP1_CI_AS": 0.94, "__other__": 0.06},
            "transparentDataEncryption": {"Enabled": 0.88, "Disabled": 0.12},
        },
        "sku_distributions": {
            "name": {"GP_Gen5_2": 0.30, "GP_Gen5_4": 0.20, "S0": 0.24, "S1": 0.14, "BC_Gen5_2": 0.12}
        },
    },
    "Microsoft.ManagedIdentity/userAssignedIdentities": {
        "frequency": 0.030,
        "property_distributions": {"usagePattern": {"workload": 0.72, "platform": 0.28}},
    },
    "Microsoft.Insights/components": {
        "frequency": 0.028,
        "property_distributions": {
            "Flow_Type": {"Bluefield": 0.96, "Redfield": 0.04},
            "publicNetworkAccessForIngestion": {"Enabled": 0.85, "Disabled": 0.15},
            "DisableLocalAuth": {"false": 0.79, "true": 0.21},
        },
    },
    "Microsoft.Sql/servers": {
        "frequency": 0.023,
        "property_distributions": {
            "version": {"12.0": 1.0},
            "publicNetworkAccess": {"Enabled": 0.63, "Disabled": 0.37},
            "minimalTlsVersion": {"1.2": 0.87, "1.0": 0.08, "1.1": 0.05},
        },
    },
    "Microsoft.Network/privateEndpoints": {
        "frequency": 0.022,
        "property_distributions": {"provisioningState": {"Succeeded": 0.99, "Failed": 0.01}},
    },
    "Microsoft.Compute/availabilitySets": {
        "frequency": 0.020,
        "property_distributions": {},
    },
    "Microsoft.Network/routeTables": {
        "frequency": 0.019,
        "property_distributions": {},
    },
    "Microsoft.OperationalInsights/workspaces": {
        "frequency": 0.018,
        "property_distributions": {
            "publicNetworkAccessForIngestion": {"Enabled": 0.88, "Disabled": 0.12},
            "retentionInDays": {"30": 0.52, "90": 0.28, "180": 0.12, "365": 0.08},
        },
    },
    "Microsoft.Insights/actionGroups": {
        "frequency": 0.017,
        "property_distributions": {},
    },
    "Microsoft.ContainerService/managedClusters": {
        "frequency": 0.016,
        "property_distributions": {
            "kubernetesVersion": {"1.29": 0.34, "1.28": 0.30, "1.30": 0.22, "1.27": 0.14},
            "enableRBAC": {"true": 0.92, "false": 0.08},
            "networkPlugin": {"azure": 0.68, "kubenet": 0.32},
        },
    },
    "Microsoft.Compute/virtualMachines/extensions": {
        "frequency": 0.015,
        "property_distributions": {
            "publisher": {
                "Microsoft.Azure.Monitor": 0.36,
                "Microsoft.Azure.Security": 0.24,
                "Microsoft.Compute": 0.22,
                "__other__": 0.18,
            },
            "autoUpgradeMinorVersion": {"true": 0.88, "false": 0.12},
            "enableAutomaticUpgrade": {"true": 0.55, "false": 0.45},
        },
    },
    "Microsoft.Insights/scheduledQueryRules": {
        "frequency": 0.014,
        "property_distributions": {},
    },
    "Microsoft.Network/privateDnsZones": {
        "frequency": 0.013,
        "property_distributions": {},
    },
    "Microsoft.RecoveryServices/vaults": {
        "frequency": 0.012,
        "property_distributions": {},
        "sku_distributions": {"name": {"RS0": 1.0}},
    },
    "Microsoft.ContainerRegistry/registries": {
        "frequency": 0.011,
        "property_distributions": {},
        "sku_distributions": {"name": {"Premium": 0.46, "Standard": 0.42, "Basic": 0.12}},
    },
    "Microsoft.Insights/activityLogAlerts": {
        "frequency": 0.011,
        "property_distributions": {},
    },
    "Microsoft.ServiceBus/namespaces": {
        "frequency": 0.010,
        "property_distributions": {},
        "sku_distributions": {"name": {"Standard": 0.58, "Premium": 0.28, "Basic": 0.14}},
    },
    "Microsoft.DataFactory/factories": {
        "frequency": 0.009,
        "property_distributions": {},
    },
    "Microsoft.Automation/automationAccounts": {
        "frequency": 0.008,
        "property_distributions": {},
    },
    "Microsoft.EventHub/namespaces": {
        "frequency": 0.008,
        "property_distributions": {},
        "sku_distributions": {"name": {"Standard": 0.66, "Basic": 0.20, "Premium": 0.14}},
    },
    "Microsoft.Network/azureFirewalls": {
        "frequency": 0.007,
        "property_distributions": {},
    },
    "Microsoft.Logic/workflows": {
        "frequency": 0.007,
        "property_distributions": {},
    },
    "Microsoft.Databricks/workspaces": {
        "frequency": 0.006,
        "property_distributions": {},
    },
    "Microsoft.EventGrid/topics": {
        "frequency": 0.006,
        "property_distributions": {},
    },
    "Microsoft.Maintenance/maintenanceConfigurations": {
        "frequency": 0.005,
        "property_distributions": {
            "configurationType": {"InGuestPatch": 0.72, "Host": 0.28},
            "visibility": {"Custom": 0.95, "Public": 0.05},
        },
    },
    "Microsoft.ApiManagement/service": {
        "frequency": 0.005,
        "property_distributions": {},
        "sku_distributions": {"name": {"Developer": 0.48, "Standard": 0.34, "Premium": 0.18}},
    },
    "Microsoft.DevCenter/devcenters": {
        "frequency": 0.004,
        "property_distributions": {},
    },
    "Microsoft.DevCenter/projects": {
        "frequency": 0.004,
        "property_distributions": {},
    },
    "Microsoft.Synapse/workspaces": {
        "frequency": 0.003,
        "property_distributions": {},
    },
    "Microsoft.DocumentDB/databaseAccounts": {
        "frequency": 0.003,
        "property_distributions": {},
    },
    "Microsoft.Cache/Redis": {
        "frequency": 0.003,
        "property_distributions": {},
        "sku_distributions": {"name": {"Standard": 0.54, "Basic": 0.28, "Premium": 0.18}},
    },
}

# --------------------------------------------------------------------------
# Tag distributions. Conventional Azure governance tag keys; every VALUE is a
# generic placeholder so no vocabulary from any organization is implied.
# --------------------------------------------------------------------------
TAG_KEYS = {
    "environment": 0.84,
    "costCenter": 0.62,
    "owner": 0.55,
    "application": 0.48,
    "team": 0.41,
    "project": 0.34,
    "businessUnit": 0.28,
    "managedBy": 0.24,
    "dataClassification": 0.18,
    "createdBy": 0.16,
    "expiresOn": 0.07,
}

TAG_VALUES = {
    "environment": {
        "production": 0.34,
        "development": 0.27,
        "test": 0.17,
        "staging": 0.12,
        "sandbox": 0.10,
    },
    "dataClassification": {
        "internal": 0.48,
        "confidential": 0.29,
        "public": 0.14,
        "restricted": 0.09,
    },
    "managedBy": {"terraform": 0.46, "bicep": 0.21, "portal": 0.19, "arm-template": 0.14},
    # Free-form keys get a generic bucketed vocabulary. The analyzer folds rare
    # values into __other__ anyway; authoring them this way makes it explicit
    # that these carry no semantic content.
    "costCenter": {f"cc-{i:04d}": round(1 / 12, 6) for i in range(1, 13)},
    "owner": {f"owner-{i}": round(1 / 10, 6) for i in range(1, 11)},
    "application": {f"app-{i}": round(1 / 16, 6) for i in range(1, 17)},
    "team": {f"team-{i}": round(1 / 12, 6) for i in range(1, 13)},
    "project": {f"project-{i}": round(1 / 14, 6) for i in range(1, 15)},
    "businessUnit": {f"bu-{i}": round(1 / 8, 6) for i in range(1, 9)},
    "createdBy": {f"principal-{i}": round(1 / 10, 6) for i in range(1, 11)},
    "expiresOn": {"__other__": 1.0},
}

TAG_COOCCURRENCE = {
    "environment": {"costCenter": 0.71, "owner": 0.63, "application": 0.55, "team": 0.46},
    "costCenter": {"environment": 0.94, "owner": 0.68, "businessUnit": 0.41},
    "owner": {"environment": 0.92, "team": 0.58, "application": 0.49},
    "application": {"environment": 0.95, "owner": 0.57, "project": 0.44},
}

UNTAGGED_RATE_BY_TYPE = {
    "Microsoft.Compute/disks": 0.42,
    "Microsoft.Network/networkInterfaces": 0.38,
    "Microsoft.Compute/virtualMachines/extensions": 0.61,
    "Microsoft.Network/publicIPAddresses": 0.29,
    "Microsoft.Compute/virtualMachines": 0.11,
    "Microsoft.Storage/storageAccounts": 0.14,
    "Microsoft.Web/sites": 0.09,
    "Microsoft.Sql/servers": 0.08,
}

# --------------------------------------------------------------------------
# Cross-subscription topology. Authored to match a hub-and-spoke landing zone.
# --------------------------------------------------------------------------
CROSS_SUB = {
    "hub_spoke": {"probability": 0.62, "hub_count": _dist_nm(3.0, 1.0)},
    "shared_keyvault": {"probability": 0.34},
    "centralized_logging": {"probability": 0.71},
    "shared_acr": {"probability": 0.26},
    "private_endpoints": {"probability": 0.31, "per_spoke_count": _dist_nm(4.0, 2.5)},
}

# --------------------------------------------------------------------------
# Governance violation rates. Authored to give a scanner a realistic mix of
# findings across severities -- enough of each to be worth testing against,
# not so many that the estate looks like a deliberately broken fixture.
# --------------------------------------------------------------------------
VIOLATIONS = {
    "STORAGE_NO_ENCRYPTION": 0.02,
    "STORAGE_PUBLIC_ACCESS": 0.06,
    "STORAGE_HTTP_ALLOWED": 0.05,
    "STORAGE_OLD_TLS": 0.08,
    "NSG_OPEN_SSH": 0.07,
    "NSG_OPEN_RDP": 0.05,
    "NSG_OPEN_ALL": 0.02,
    "KV_NO_SOFT_DELETE": 0.06,
    "KV_NO_PURGE_PROTECT": 0.14,
    "VM_NO_BACKUP": 0.18,
    "VM_PUBLIC_IP": 0.09,
    "TAG_MISSING_ENV": 0.12,
    "TAG_MISSING_OWNER": 0.19,
    "TAG_MISSING_COSTCENTER": 0.16,
    "SQL_NO_AUDIT": 0.11,
    "SQL_NO_TDE": 0.06,
    "AKS_RBAC_DISABLED": 0.04,
    "DISK_UNENCRYPTED": 0.07,
}

# --------------------------------------------------------------------------
# Cost distributions (v1.2). Lognormal monthly USD per resource type, authored
# from PUBLIC Azure list-price orders of magnitude. exp(mu) is roughly the
# median monthly spend; sigma sets the spread across SKUs and utilisation.
# These only seed the bootstrap estate -- the published profile's cost figures
# are re-fitted by the analyzer from the generated estate.
# --------------------------------------------------------------------------
COSTS = {
    "Microsoft.Compute/virtualMachines": (4.6, 1.30),
    "Microsoft.Compute/disks": (2.5, 1.05),
    "Microsoft.Storage/storageAccounts": (2.9, 1.45),
    "Microsoft.Sql/servers/databases": (5.1, 1.25),
    "Microsoft.Web/serverfarms": (4.3, 1.10),
    "Microsoft.ContainerService/managedClusters": (5.4, 1.00),
    "Microsoft.ContainerRegistry/registries": (3.0, 0.80),
    "Microsoft.Network/azureFirewalls": (6.5, 0.55),
    "Microsoft.Network/publicIPAddresses": (1.3, 0.45),
    "Microsoft.OperationalInsights/workspaces": (4.0, 1.50),
    "Microsoft.Insights/components": (2.6, 1.40),
    "Microsoft.KeyVault/vaults": (0.9, 0.85),
    "Microsoft.RecoveryServices/vaults": (3.6, 1.20),
    "Microsoft.ServiceBus/namespaces": (3.2, 1.00),
    "Microsoft.EventHub/namespaces": (3.5, 1.05),
    "Microsoft.DataFactory/factories": (3.8, 1.35),
    "Microsoft.Databricks/workspaces": (6.0, 1.30),
    "Microsoft.Synapse/workspaces": (6.2, 1.25),
    "Microsoft.DocumentDB/databaseAccounts": (4.8, 1.60),
    "Microsoft.ApiManagement/service": (5.6, 0.90),
    "Microsoft.Cache/Redis": (4.1, 0.95),
    "Microsoft.Network/privateEndpoints": (2.2, 0.35),
}


def _check_sums() -> list[str]:
    """Return a list of authoring errors (empty when the profile is coherent)."""
    errs: list[str] = []

    def near(label: str, total: float, target: float = 1.0, tol: float = 1e-6) -> None:
        if abs(total - target) > tol:
            errs.append(f"{label}: sums to {total!r}, expected {target}")

    near("subscription_archetypes.weight", sum(a["weight"] for a in SUBSCRIPTION_ARCHETYPES))
    near("resource_group_templates.weight", sum(t["weight"] for t in RG_TEMPLATES))
    near("resource_type_distributions.frequency", sum(t["frequency"] for t in TYPES.values()))

    for a in SUBSCRIPTION_ARCHETYPES:
        near(f"{a['id']}.location_distribution", sum(a["location_distribution"].values()))

    for name, entry in TYPES.items():
        for section in ("property_distributions", "sku_distributions"):
            for field, hist in (entry.get(section) or {}).items():
                near(f"{name}.{section}.{field}", sum(hist.values()), tol=1e-6)
        if entry.get("kind_distributions"):
            near(f"{name}.kind_distributions", sum(entry["kind_distributions"].values()))

    for key, hist in TAG_VALUES.items():
        near(f"tag_values.{key}", sum(hist.values()), tol=2e-3)

    misc = next(t for t in RG_TEMPLATES if t["id"] == "__misc__")
    near("__misc__.type_weights", sum(misc["type_weights"].values()))

    # Every ANCHOR_REQUIRED archetype must have its anchor reachable from at
    # least one template, or the semantic-naming gate is vacuous on this profile.
    try:
        from tenantless.generator import archetypes as arch  # noqa: PLC0415

        reachable = {t for tpl in RG_TEMPLATES for t in tpl["type_set"]}
        reachable |= set(misc["type_weights"])
        for entry in arch.ARCHETYPES:
            if entry.confirmation is arch.ConfirmationPolicy.GENERIC:
                continue
            if not (set(entry.required_any) & reachable):
                errs.append(
                    f"archetype {entry.id!r} has no anchor reachable from any "
                    f"template type_set -- semantic naming would be vacuous"
                )
    except ImportError:  # running outside the project venv; skip the cross-check
        pass

    return errs


def build() -> dict:
    return {
        "version": "1.2",
        # Fixed, not time.now(): the bootstrap is an authored artifact and must
        # rebuild byte-identically.
        "extracted_at": "2026-07-26T00:00:00Z",
        "source_stats": {
            "total_subscriptions": TOTAL_SUBSCRIPTIONS,
            "total_resource_groups": TOTAL_RESOURCE_GROUPS,
            "total_resources": TOTAL_RESOURCES,
        },
        "subscription_archetypes": SUBSCRIPTION_ARCHETYPES,
        "resource_group_templates": RG_TEMPLATES,
        "resource_type_distributions": TYPES,
        "tag_distributions": {
            "key_frequencies": TAG_KEYS,
            "value_distributions": TAG_VALUES,
            "key_cooccurrence": TAG_COOCCURRENCE,
            "value_cardinality": {k: len(v) for k, v in TAG_VALUES.items()},
            "untagged_rate_by_type": UNTAGGED_RATE_BY_TYPE,
        },
        "cross_subscription_dependencies": CROSS_SUB,
        "governance_violations": {"type_frequencies": VIOLATIONS},
        "cost_distributions": {
            t: {"distribution": "lognormal", "mu": mu, "sigma": sigma, "sample_count": 0}
            for t, (mu, sigma) in COSTS.items()
        },
        "provenance": {
            "reviewed": True,
            "source": "hand-authored",
            "extracted_by": "scripts/build_oss_bootstrap_profile.py",
            "synthetic": True,
            "derived_from_real_tenant": False,
        },
    }


def main() -> int:
    errs = _check_sums()
    if errs:
        print("Authoring errors -- refusing to write:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1

    profile = build()

    try:
        from tenantless.analyzer.schema_validate import validate_profile

        validate_profile(profile)
        validated = "validated against profiles/schema.json"
    except ImportError:
        validated = "SCHEMA NOT VALIDATED (tenantless not importable)"

    OUT.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {OUT.relative_to(REPO)}: "
        f"{len(SUBSCRIPTION_ARCHETYPES)} archetypes, "
        f"{len(RG_TEMPLATES)} RG templates, {len(TYPES)} resource types, "
        f"{len(TAG_KEYS)} tag keys, {len(VIOLATIONS)} violation codes, "
        f"{len(COSTS)} cost distributions -- {validated}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
