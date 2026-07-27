"""Build a tiny synthetic DuckDB fixture with KNOWN distributions for CI.

This is the CI-safe stand-in for the external real scanner.duckdb. No
committed test may depend on the real DB; every analyzer test runs against this
fixture instead.

Known facts the tests rely on:
- ``resources.type`` contains a deliberately LOW-count type
  (``microsoft.test/raretype`` -- exactly 4 rows) to prove min-bucket dropping
  at the default ``min_bucket_size=5``.
- A clearly dominant HIGH-count type (``microsoft.compute/virtualmachines``)
  survives min-bucketing with a normalized frequency in (0, 1].
- Two FAKE-but-real-looking identifier strings are embedded so the denylist
  test has material to scan for:
    * ``FAKE-HUB-EMEA-PROD``  (a fake subscription display name)
    * ``fake-vm-payroll-007`` (a fake resource name)
  These strings are returned by :func:`fake_identifiers` so tests can seed a
  denylist without hardcoding them in two places.

K-means / RG-template signal (added for Plan 01.1-02):
- ``N_SUBSCRIPTIONS`` (8) subscriptions across two clearly separable shapes:
    * "big" subs: many RGs, many resources, mostly ``eastus2``;
    * "small" subs: few RGs, few resources, mostly ``westeurope``.
  This separation lets k-means recover distinct archetypes; 8 subscriptions is
  enough to request k up to 8 (the acceptance test uses N=3 and N=6).
- Resource groups carry repeatable TYPE-SET compositions (``net``, ``compute``,
  ``data``) plus one deliberately RARE composition (``RARE_RG_COMPOSITION``)
  that occurs fewer than ``min_bucket_size`` times so it must fold into the
  ``__misc__`` RG template.
- A deliberately RARE location (``RARE_LOCATION`` -- exactly 2 resources) proves
  the per-archetype/global location min-bucket merge into ``__other__``.

The writable builder intentionally does NOT use ``read_only=True`` (it creates
the file); the production reader path always opens ``read_only=True``.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

# Fake-but-real-looking identifiers embedded in the fixture data. Exposed so the
# denylist test seeds its denylist from the single source of truth.
FAKE_SUB_DISPLAY_NAME = "FAKE-HUB-EMEA-PROD"
FAKE_RESOURCE_NAME = "fake-vm-payroll-007"

# The low-count resource type that MUST be dropped at min_bucket_size=5.
RARE_TYPE = "microsoft.test/raretype"
RARE_TYPE_COUNT = 4

# A dominant high-count type that MUST survive min-bucketing.
COMMON_TYPE = "microsoft.compute/virtualmachines"

# Canonical resource types used to compose RG type-sets (lowercase source casing,
# matching the real scan; the extractor canonicalizes the leading namespace).
TYPE_VNET = "microsoft.network/virtualnetworks"
TYPE_NIC = "microsoft.network/networkinterfaces"
TYPE_VM = COMMON_TYPE
TYPE_DISK = "microsoft.compute/disks"
TYPE_SA = "microsoft.storage/storageaccounts"
TYPE_SQL = "microsoft.sql/servers/databases"

# A deliberately rare RG composition (a single RG) that must fold into __misc__.
RARE_RG_COMPOSITION = (RARE_TYPE,)

# Locations. The dominant ones survive; RARE_LOCATION has only 2 occurrences and
# must merge into "__other__" at min_bucket_size=5.
LOC_WESTEUROPE = "westeurope"
LOC_EASTUS2 = "eastus2"
RARE_LOCATION = "antarcticasouth"
RARE_LOCATION_COUNT = 2

# Number of subscriptions in the fixture. Sized so k up to this value is testable.
N_SUBSCRIPTIONS = 8

# Privacy bucket-size the fixture is calibrated against.
MIN_BUCKET_SIZE = 5

# --- Tag-value signals (Plan 01.1-03 Task 1) ---------------------------------
# Every "big"/"small" resource carries an "Environment" tag whose value is a
# generic enum ("prod"/"dev") -- safe, above-threshold. We ALSO attach a
# deliberately RARE tag value to a single resource so the tag-value min-bucket
# merge into "__other__" is provable (a real-looking owner string below
# threshold MUST NOT leak into the output profile).
RARE_TAG_KEY = "Owner"
RARE_TAG_VALUE = "fake-owner-jdoe-secret"  # real-looking, below threshold

# --- Per-type property/sku JSON shapes (Plan 01.1-03 Task 1) -----------------
# VM resources carry a known properties/sku JSON so type_shapes can aggregate
# enum/bool/size field-value frequencies for the top types. Values are generic
# enums/sizes (never identifiers).
VM_PROPERTIES = '{"vmSize":"Standard_D2s_v3","osType":"Linux","provisioningState":"Succeeded"}'
VM_SKU = '{"name":"Standard_D2s_v3","tier":"Standard"}'
SA_PROPERTIES = '{"accessTier":"Hot","supportsHttpsTrafficOnly":true,"minimumTlsVersion":"TLS1_2"}'
SA_SKU = '{"name":"Standard_LRS","tier":"Standard"}'

# --- Cross-subscription reference signal (Plan 01.1-03 Task 2) ---------------
# A handful of resources in "small" subscriptions carry a private-endpoint-style
# property whose target resource id points at a DIFFERENT subscription (the hub,
# sub-0000) -- this is the cross-sub reference the cross_sub extractor detects.
HUB_SUB_ID = "sub-0000"

# --- Findings signal (Plan 01.1-03 Task 2) -----------------------------------
# finding_type -> count in the fixture; counts below MIN_BUCKET_SIZE are dropped,
# unmapped finding_types are dropped regardless.
FINDING_ABOVE_THRESHOLD = "unattached_nics"  # x8, maps to a known VIOL_* type
FINDING_BELOW_THRESHOLD = "unattached_disks"  # x2, dropped by min-bucket
FINDING_UNMAPPED = "mystery_detector"  # x5, dropped (no vocabulary mapping)


def fake_identifiers() -> list[str]:
    """Return the fake real-looking identifiers embedded in the fixture."""
    return [FAKE_SUB_DISPLAY_NAME, FAKE_RESOURCE_NAME, RARE_TAG_VALUE]


def build_fixture(path: str | Path) -> Path:
    """Create a small DuckDB at ``path`` with known tables/distributions.

    Returns the path written.
    """
    path = Path(path)
    if path.exists():
        path.unlink()

    conn = duckdb.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE subscriptions (
                scan_id VARCHAR,
                subscription_id VARCHAR,
                display_name VARCHAR,
                state VARCHAR,
                tags JSON
            );
            CREATE TABLE resource_groups (
                scan_id VARCHAR,
                resource_group_id VARCHAR,
                name VARCHAR,
                location VARCHAR,
                subscription_id VARCHAR,
                tags JSON
            );
            CREATE TABLE resources (
                scan_id VARCHAR,
                resource_id VARCHAR,
                name VARCHAR,
                type VARCHAR,
                location VARCHAR,
                resource_group VARCHAR,
                subscription_id VARCHAR,
                properties JSON,
                sku JSON,
                tags JSON,
                kind VARCHAR
            );
            CREATE TABLE findings (
                scan_id VARCHAR,
                resource_id VARCHAR,
                finding_type VARCHAR,
                category VARCHAR,
                severity VARCHAR
            );
            """
        )

        sub_rows: list[tuple] = []
        rg_rows: list[tuple] = []
        res_rows: list[tuple] = []

        # Resource counter for globally-unique ids.
        rc = 0

        def add_resource(
            name: str,
            rtype: str,
            location: str,
            rg: str,
            sub: str,
            tags: str = "{}",
            properties: str = "{}",
            sku: str = "{}",
        ) -> None:
            nonlocal rc
            res_rows.append(
                (
                    "scan1",
                    f"res-{rc}",
                    name,
                    rtype,
                    location,
                    rg,
                    sub,
                    properties,
                    sku,
                    tags,
                    None,
                )
            )
            rc += 1

        # Two archetype shapes, deterministic and clearly separable.
        # "big": 4 RGs, ~12 resources, mostly eastus2, denser tags.
        # "small": 2 RGs, ~5 resources, mostly westeurope, sparser tags.
        # First 4 subscriptions are "big", last 4 are "small".
        BIG = "big"
        SMALL = "small"
        shapes = [BIG, BIG, BIG, BIG, SMALL, SMALL, SMALL, SMALL]
        assert len(shapes) == N_SUBSCRIPTIONS

        # Repeatable RG type-set compositions (each a sorted tuple of types).
        comp_net = [TYPE_VNET, TYPE_NIC]
        comp_compute = [TYPE_VM, TYPE_DISK]
        comp_data = [TYPE_SA, TYPE_SQL]

        for s in range(N_SUBSCRIPTIONS):
            sub = f"sub-{s:04d}"
            shape = shapes[s]
            # Embed the fake display name in exactly one subscription.
            display = FAKE_SUB_DISPLAY_NAME if s == 0 else f"workload-{shape}-{s}"
            # Denser tags for big subs (more tag keys -> higher tag_density).
            sub_tags = (
                '{"Environment":"prod","BU":"finance","CostCenter":"100"}'
                if shape == BIG
                else '{"Environment":"dev"}'
            )
            sub_rows.append(("scan1", sub, display, "Enabled", sub_tags))

            loc = LOC_EASTUS2 if shape == BIG else LOC_WESTEUROPE
            res_tags = (
                '{"Environment":"prod","BU":"finance"}'
                if shape == BIG
                else '{"Environment":"dev"}'
            )

            if shape == BIG:
                compositions = [comp_net, comp_compute, comp_data, comp_compute]
            else:
                compositions = [comp_net, comp_compute]

            # Mild per-subscription variation (s % 3) so same-shape subscriptions
            # are NOT identical feature points -- this lets k-means recover up to
            # ~6 distinct clusters from 8 subscriptions without degenerate ties.
            per_type = 2 + (s % 3)

            for r_idx, comp in enumerate(compositions):
                rg = f"rg-{s}-{r_idx}"
                rg_rows.append(("scan1", f"rgid-{s}-{r_idx}", rg, loc, sub, sub_tags))
                # Each composition: ``per_type`` resources of each type in the set.
                for t in comp:
                    for _ in range(per_type):
                        nm = (
                            FAKE_RESOURCE_NAME
                            if (s == 0 and r_idx == 1 and t == TYPE_VM and rc % 2 == 0)
                            else f"res-{shape}-{rc}"
                        )
                        # Per-type property/sku JSON for the type_shapes extractor.
                        if t == TYPE_VM:
                            props, sku = VM_PROPERTIES, VM_SKU
                        elif t == TYPE_SA:
                            props, sku = SA_PROPERTIES, SA_SKU
                        else:
                            props, sku = "{}", "{}"
                        # Cross-subscription reference: small subs' NICs point a
                        # private-endpoint-style property at the hub subscription.
                        if shape == SMALL and t == TYPE_NIC:
                            props = (
                                '{"privateEndpointConnections":['
                                '{"id":"/subscriptions/' + HUB_SUB_ID
                                + '/resourceGroups/hub/providers/'
                                'Microsoft.Network/privateEndpoints/pe-x"}]}'
                            )
                        add_resource(
                            nm, t, loc, rg, sub, res_tags, properties=props, sku=sku
                        )

        # --- Deliberately RARE signals, attached to sub-0000 (a "big" sub) ---

        # Rare RG composition: a single RG holding only the rare type (one RG ->
        # below min_bucket_size -> folds into __misc__). Also supplies the
        # RARE_TYPE_COUNT (4) low-count type rows for the min-bucket type test.
        rare_rg = "rg-rare"
        rg_rows.append(("scan1", "rgid-rare", rare_rg, LOC_EASTUS2, "sub-0000", "{}"))
        for _ in range(RARE_TYPE_COUNT):
            add_resource(f"rare-{rc}", RARE_TYPE, LOC_EASTUS2, rare_rg, "sub-0000")

        # Rare location: a couple of resources in an otherwise-unused region so the
        # location min-bucket merge into "__other__" is provable.
        for _ in range(RARE_LOCATION_COUNT):
            add_resource(
                f"odd-{rc}", TYPE_SA, RARE_LOCATION, "rg-0-2", "sub-0000"
            )

        # Rare TAG VALUE: a single resource carries an Owner tag with a
        # real-looking value seen exactly once (< min_bucket_size). The tag KEY
        # ("Owner") may cross the boundary, but the VALUE must fold into
        # "__other__" and never leak into the output profile.
        rare_tag_json = (
            '{"Environment":"prod","' + RARE_TAG_KEY + '":"' + RARE_TAG_VALUE + '"}'
        )
        add_resource(
            f"res-tagged-{rc}", TYPE_SA, LOC_EASTUS2, "rg-0-2", "sub-0000",
            tags=rare_tag_json, properties=SA_PROPERTIES, sku=SA_SKU,
        )

        conn.executemany(
            "INSERT INTO subscriptions VALUES (?,?,?,?,?)", sub_rows
        )
        conn.executemany(
            "INSERT INTO resource_groups VALUES (?,?,?,?,?,?)", rg_rows
        )
        conn.executemany(
            "INSERT INTO resources VALUES (?,?,?,?,?,?,?,?,?,?,?)", res_rows
        )

        # Findings: a small histogram by finding_type so the violations
        # extractor can map real finding_type values to the simulator violation
        # vocabulary and emit normalized rates. ``unattached_disks`` appears
        # below MIN_BUCKET_SIZE so it is dropped; ``unattached_nics`` and
        # ``stopped_vms`` are above threshold and map to known VIOL_* types.
        # ``mystery_detector`` is unmapped and must be silently dropped.
        finding_rows: list[tuple] = []
        for i in range(8):  # unattached_nics x8 (above threshold)
            finding_rows.append(
                ("scan1", f"res-{i}", "unattached_nics", "cost", "low")
            )
        for i in range(6):  # stopped_vms x6 (above threshold)
            finding_rows.append(
                ("scan1", f"res-{i}", "stopped_vms", "cost", "medium")
            )
        for i in range(2):  # unattached_disks x2 (BELOW threshold -> dropped)
            finding_rows.append(
                ("scan1", f"res-{i}", "unattached_disks", "cost", "low")
            )
        for i in range(5):  # unmapped finding_type -> dropped regardless
            finding_rows.append(
                ("scan1", f"res-{i}", "mystery_detector", "other", "low")
            )
        conn.executemany("INSERT INTO findings VALUES (?,?,?,?,?)", finding_rows)
    finally:
        conn.close()

    return path


if __name__ == "__main__":  # pragma: no cover - manual fixture build
    out = build_fixture(Path(__file__).with_name("fixture.duckdb"))
    print(f"Built fixture DuckDB at {out}")
