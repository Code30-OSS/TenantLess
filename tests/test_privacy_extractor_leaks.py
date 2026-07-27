"""Structural regression tests for the privacy-extractor-leaks fix.

These tests are INDEPENDENT of the gitignored denylist backstop. They feed the
extractors real-data-shaped pathological inputs (the exact patterns that the
first real source-scan run leaked) and assert that NONE survive to the output:

    1. a ``hidden-link:/subscriptions/...`` tag KEY (full resource id as key),
    2. a ``databricks-instance-name`` 32-hex tag VALUE repeated >= 5x (survives
       the count-based min-bucket merge, so it must be shape-gated),
    3. an UNLISTED top resource type carrying ``id`` / ``ConnectionString``
       property fields (the type_shapes allow-all-on-None bug).

The synthetic CI fixture lacked these patterns, which is why prior tests passed
while the real run tripped the denylist gate. This module closes that gap.
"""

from __future__ import annotations

import json

import polars as pl

from tenantless.analyzer import privacy
from tenantless.analyzer.extractors import cooccurrence, tags, type_shapes


# --- Pathological real-shaped values (synthetic, but structurally identical) --
# Every literal below is invented. The point of these fixtures is their SHAPE --
# a full resource id as a tag key, a 32-hex instance id, a person-shaped Owner
# value -- so nothing real is needed, and a test suite whose subject is the data
# boundary is the last place a real identifier should be pasted into.
LEAK_SUB_UUID = "b0a1c2d3-1111-2222-3333-444455556666"
HIDDEN_LINK_KEY = (
    "hidden-link:/subscriptions/" + LEAK_SUB_UUID
    + "/resourceGroups/acme-data-uat-rg/providers/"
    "Microsoft.Web/serverFarms/ASP-acme-uat"
)
DATABRICKS_HEX_VALUE = "a1b2c3d4e5f6071829304a5b6c7d8e9f"  # 32 hex chars
LEAK_CONNECTION_STRING = (
    "InstrumentationKey=" + LEAK_SUB_UUID + ";IngestionEndpoint=https://x"
)
LEAK_ID_BLOB = "/subscriptions/" + LEAK_SUB_UUID + "/resourceGroups/rg/x"


def test_hidden_link_tag_key_dropped():
    """A hidden-link:/subscriptions/... key must NOT reach key_frequencies."""
    key_counts = pl.DataFrame(
        {
            "tag_key": ["Environment", HIDDEN_LINK_KEY],
            "count": [100, 40],
        }
    )
    value_counts = pl.DataFrame(
        {"tag_key": [], "tag_value": [], "count": []},
        schema={"tag_key": pl.Utf8, "tag_value": pl.Utf8, "count": pl.Int64},
    )
    out = tags.extract(key_counts, value_counts, total_resources=200)

    assert "Environment" in out["key_frequencies"]
    assert HIDDEN_LINK_KEY not in out["key_frequencies"]
    # The subscription UUID must not appear ANYWHERE in the fragment.
    assert LEAK_SUB_UUID not in json.dumps(out)


def test_identifier_key_emits_frequency_but_no_value_map():
    """An identifier-bearing key (NOT on the value allowlist) keeps its
    key_frequency but emits NO value_distribution (DECISION 1: positive value
    allowlist). ``databricks-instance-name`` is a safe-SHAPED key (no path/UUID
    in the key itself) yet its VALUES are real instance ids, so values are
    dropped wholesale rather than relying on per-value shape heuristics."""
    key_counts = pl.DataFrame(
        {"tag_key": ["databricks-instance-name"], "count": [50]}
    )
    value_counts = pl.DataFrame(
        {
            "tag_key": ["databricks-instance-name", "databricks-instance-name"],
            "tag_value": [DATABRICKS_HEX_VALUE, "shared-pool"],
            "count": [50, 30],
        }
    )
    out = tags.extract(key_counts, value_counts, total_resources=200)

    # The key is a legitimate (safe-shaped) key, so key_frequencies records it.
    assert "databricks-instance-name" in out["key_frequencies"]
    # But it is NOT on VALUE_ALLOWLIST_KEYS -> no value map at all.
    assert "databricks-instance-name" not in out["value_distributions"]
    # The hex id must never appear anywhere in the fragment.
    assert DATABRICKS_HEX_VALUE not in json.dumps(out)


def test_owner_key_emits_frequency_but_no_value_map():
    """``Owner`` carries real person names / emails and is NOT on the value
    allowlist: it must emit a key_frequency but NO value map."""
    key_counts = pl.DataFrame({"tag_key": ["Owner"], "count": [80]})
    value_counts = pl.DataFrame(
        {
            "tag_key": ["Owner", "Owner"],
            "tag_value": ["Dana Okonkwo", "platform-team@example.invalid"],
            "count": [50, 30],
        }
    )
    out = tags.extract(key_counts, value_counts, total_resources=200)

    assert "Owner" in out["key_frequencies"]
    assert "Owner" not in out["value_distributions"]
    assert "Dana Okonkwo" not in json.dumps(out)
    assert "platform-team@example.invalid" not in json.dumps(out)


def test_migrate_project_key_emits_frequency_but_no_value_map():
    """``Migrate Project`` carries real RG/project names and is NOT on the value
    allowlist."""
    key_counts = pl.DataFrame({"tag_key": ["Migrate Project"], "count": [40]})
    value_counts = pl.DataFrame(
        {
            "tag_key": ["Migrate Project", "Migrate Project"],
            "tag_value": ["acme-common-nprd-use2-aks-rg", "acmedbpoc"],
            "count": [25, 20],
        }
    )
    out = tags.extract(key_counts, value_counts, total_resources=200)

    assert "Migrate Project" in out["key_frequencies"]
    assert "Migrate Project" not in out["value_distributions"]
    assert "acme-common-nprd-use2-aks-rg" not in json.dumps(out)
    assert "acmedbpoc" not in json.dumps(out)


def test_system_and_namespaced_tag_keys_dropped():
    """``__SYSTEM__<Service>_<resource-name>`` keys (embedding a real resource
    name) and custom ``<tenant>:<suffix>`` namespaced keys (whose prefix is the
    tenant/org name) must NOT reach key_frequencies. Generic governance keys and
    Azure type-style keys are kept."""
    sys_key = "__SYSTEM__AzureOpenAI_acme-prod-deployment_aoai"
    tenant_key = "acmecorp:application"
    key_counts = pl.DataFrame(
        {
            "tag_key": ["Environment", sys_key, tenant_key],
            "count": [100, 30, 25],
        }
    )
    value_counts = pl.DataFrame(
        {"tag_key": [], "tag_value": [], "count": []},
        schema={"tag_key": pl.Utf8, "tag_value": pl.Utf8, "count": pl.Int64},
    )
    out = tags.extract(key_counts, value_counts, total_resources=200)

    assert "Environment" in out["key_frequencies"]
    assert sys_key not in out["key_frequencies"]
    assert tenant_key not in out["key_frequencies"]
    # Neither the embedded resource name nor the tenant token leaks anywhere.
    assert "acme-prod-deployment" not in json.dumps(out)
    assert "acmecorp" not in json.dumps(out)


def test_governance_key_emits_value_map():
    """A known governance key (``Environment``) on the allowlist emits its
    bounded enum value_distribution."""
    key_counts = pl.DataFrame({"tag_key": ["Environment"], "count": [120]})
    value_counts = pl.DataFrame(
        {
            "tag_key": ["Environment", "Environment", "Environment"],
            "tag_value": ["prod", "dev", "uat"],
            "count": [60, 40, 20],
        }
    )
    out = tags.extract(key_counts, value_counts, total_resources=200)

    assert "Environment" in out["key_frequencies"]
    dist = out["value_distributions"].get("Environment", {})
    assert "prod" in dist
    assert "dev" in dist
    assert "uat" in dist


def test_allowlisted_key_still_shape_gates_identifier_values():
    """Defense-in-depth: even for an allowlisted key, an identifier-shaped value
    (32-hex) folds into __other__ rather than appearing verbatim."""
    # ``Backup`` is on the allowlist; seed it with one enum value + one hex id.
    key_counts = pl.DataFrame({"tag_key": ["Backup"], "count": [80]})
    value_counts = pl.DataFrame(
        {
            "tag_key": ["Backup", "Backup"],
            "tag_value": ["Enabled", DATABRICKS_HEX_VALUE],
            "count": [50, 30],
        }
    )
    out = tags.extract(key_counts, value_counts, total_resources=200)

    dist = out["value_distributions"].get("Backup", {})
    assert "Enabled" in dist
    # The hex id folds to __other__ despite repeating 30x.
    assert DATABRICKS_HEX_VALUE not in dist
    assert DATABRICKS_HEX_VALUE not in json.dumps(out)
    assert "__other__" in dist


def test_unlisted_type_property_fields_denied():
    """An unlisted top type must contribute NO property fields (deny-all on None)."""
    # A type NOT in PROPERTY_FIELD_ALLOWLIST.
    unlisted_key = "Microsoft.someunlisted/widgets"
    assert unlisted_key not in type_shapes.PROPERTY_FIELD_ALLOWLIST

    rtd = {unlisted_key: {"frequency": 1.0, "property_distributions": {}}}

    def property_frame_for(_raw_type: str) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "field": ["id", "ConnectionString", "id"],
                "value": [LEAK_ID_BLOB, LEAK_CONNECTION_STRING, LEAK_ID_BLOB],
                "count": [60, 60, 60],
            }
        )

    def sku_frame_for(_raw_type: str) -> pl.DataFrame:
        return pl.DataFrame(
            {"field": [], "value": [], "count": []},
            schema={"field": pl.Utf8, "value": pl.Utf8, "count": pl.Int64},
        )

    type_shapes.extract_into(
        rtd,
        property_frame_for=property_frame_for,
        sku_frame_for=sku_frame_for,
        min_bucket_size=5,
    )

    # No property fields emitted for the unlisted type.
    assert rtd[unlisted_key]["property_distributions"] == {}
    # Neither the id blob nor the connection string leaks anywhere.
    assert LEAK_ID_BLOB not in json.dumps(rtd)
    assert LEAK_CONNECTION_STRING not in json.dumps(rtd)


def test_listed_type_still_emits_safe_enum_fields():
    """A newly allow-listed real type emits its curated enum fields, not ids."""
    listed_key = "Microsoft.insights/components"
    allowed = type_shapes.PROPERTY_FIELD_ALLOWLIST[listed_key]
    assert "ConnectionString" not in allowed  # identifier field excluded
    assert "AppId" not in allowed
    assert "TenantId" not in allowed

    rtd = {listed_key: {"frequency": 1.0, "property_distributions": {}}}

    def property_frame_for(_raw_type: str) -> pl.DataFrame:
        return pl.DataFrame(
            {
                # One allow-listed enum field + two identifier fields.
                "field": ["Flow_Type", "ConnectionString", "AppId"],
                "value": ["Bluefield", LEAK_CONNECTION_STRING, LEAK_SUB_UUID],
                "count": [40, 40, 40],
            }
        )

    def sku_frame_for(_raw_type: str) -> pl.DataFrame:
        return pl.DataFrame(
            {"field": [], "value": [], "count": []},
            schema={"field": pl.Utf8, "value": pl.Utf8, "count": pl.Int64},
        )

    type_shapes.extract_into(
        rtd,
        property_frame_for=property_frame_for,
        sku_frame_for=sku_frame_for,
        min_bucket_size=5,
    )

    props = rtd[listed_key]["property_distributions"]
    # The safe enum field survives; identifier fields are dropped.
    assert set(props.keys()) <= allowed
    assert "Flow_Type" in props
    assert "ConnectionString" not in props
    assert LEAK_CONNECTION_STRING not in json.dumps(rtd)
    assert LEAK_SUB_UUID not in json.dumps(rtd)


def test_path_shaped_value_folded():
    """A resource-path tag VALUE folds into __other__ regardless of count.

    Uses an allowlisted key (``Environment``) so the value map is emitted at all;
    the path-shaped value must still fold to __other__ (defense-in-depth)."""
    key_counts = pl.DataFrame({"tag_key": ["Environment"], "count": [60]})
    value_counts = pl.DataFrame(
        {
            "tag_key": ["Environment", "Environment"],
            "tag_value": [LEAK_ID_BLOB, "prod"],
            "count": [30, 30],
        }
    )
    out = tags.extract(key_counts, value_counts, total_resources=100)
    dist = out["value_distributions"].get("Environment", {})
    assert LEAK_ID_BLOB not in dist
    assert LEAK_SUB_UUID not in json.dumps(out)
    assert "prod" in dist
    assert "__other__" in dist


# --- ANLZ-07 co-occurrence / cardinality leak path (CR-01 regression) ---------
# The reader pair/value-count frames feed RAW, unfiltered tag keys into these
# extractors -- the only place the _is_identifier_shaped_key guard runs for this
# path is INSIDE the extractor. Before the CR-01 fix, a
# hidden-link:/subscriptions/<uuid>/... key surfaced verbatim as a DICT KEY in
# key_cooccurrence / value_cardinality. These tests would FAIL against the
# pre-fix code and lock the leak class out for good (WR-01).


def test_tag_key_cooccurrence_drops_identifier_shaped_key():
    """An identifier-shaped tag key must NEVER become a key in the co-occurrence
    matrix -- it embeds a real subscription UUID + resource id."""
    # The hidden-link key co-occurs with a legit governance key on enough
    # resources to clear the min-bucket floor, so only the SHAPE guard can drop
    # it (not the count gate).
    pair_counts = pl.DataFrame(
        {
            "key_a": ["Environment", HIDDEN_LINK_KEY],
            "key_b": ["Owner", "Owner"],
            "count": [40, 40],
        }
    )
    result = cooccurrence.tag_key_cooccurrence(pair_counts, min_bucket_size=5)

    # The legit pair survives; the identifier-shaped key appears NOWHERE -- not as
    # a source key, not as a target key.
    assert "Environment" in result
    assert HIDDEN_LINK_KEY not in result
    for targets in result.values():
        assert HIDDEN_LINK_KEY not in targets
    blob = json.dumps(result)
    assert HIDDEN_LINK_KEY not in blob
    assert LEAK_SUB_UUID not in blob
    # The denylist backstop (seeded from the same UUID) must NOT trip -- the
    # structural guard holds independent of it.
    privacy.scan_denylist(result, [LEAK_SUB_UUID])


def test_tag_value_cardinality_drops_identifier_shaped_key():
    """An identifier-shaped tag key must NEVER become a key in the value
    cardinality map (the cardinality is keyed BY tag_key)."""
    value_counts = pl.DataFrame(
        {
            "tag_key": [
                "Environment",
                "Environment",
                HIDDEN_LINK_KEY,
                HIDDEN_LINK_KEY,
            ],
            "tag_value": ["prod", "dev", "a", "b"],
            "count": [40, 30, 20, 20],
        }
    )
    result = cooccurrence.tag_value_cardinality(value_counts, min_bucket_size=5)

    # The legit key is counted; the identifier-shaped key is absent entirely
    # (not even seeded at 0).
    assert "Environment" in result
    assert HIDDEN_LINK_KEY not in result
    blob = json.dumps(result)
    assert HIDDEN_LINK_KEY not in blob
    assert LEAK_SUB_UUID not in blob
    privacy.scan_denylist(result, [LEAK_SUB_UUID])


def test_cooccurrence_does_not_collide_rgs_across_subscriptions():
    """CR-02 regression: identically-named RGs in different subscriptions must
    NOT be joined into the same RG (which would fabricate co-occurrences)."""
    # sub-A/networking holds {vnet}; sub-B/networking holds {vm}. With RG-name-
    # only join these would falsely co-occur. Scoped by subscription they share
    # no RG, so NO pair survives.
    frame = pl.DataFrame(
        {
            "subscription_id": ["sub-A", "sub-B"],
            "resource_group": ["networking", "networking"],
            "type": [
                "microsoft.network/virtualnetworks",
                "microsoft.compute/virtualmachines",
            ],
        }
    )
    result = cooccurrence.extract(frame, min_bucket_size=1)
    # vnet and vm never shared a (subscription, RG), so no co-occurrence exists.
    assert result == {}
