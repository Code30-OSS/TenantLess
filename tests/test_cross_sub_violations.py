"""Unit tests for the cross-subscription dependency extractor and the
governance-violation extractor, plus the FULL build_profile assembly, against
the synthetic CI fixture.

NONE of these tests touch the external real DB.
"""

from __future__ import annotations

import inspect
import math

import pytest

from tenantless.analyzer import schema_validate
from tenantless.analyzer.extractors import cross_sub, violations
from tenantless.analyzer.profile import build_profile
from tenantless.analyzer.reader import open_duckdb

from fixtures.build_fixture_duckdb import MIN_BUCKET_SIZE


REQUIRED_XSUB_KEYS = {
    "hub_spoke",
    "shared_keyvault",
    "centralized_logging",
    "shared_acr",
    "private_endpoints",
}


# --------------------------------------------------------------------------- #
# cross_sub extractor
# --------------------------------------------------------------------------- #


def _all_numbers(node):
    """Yield every numeric leaf in a nested dict/list (excluding bools)."""
    if isinstance(node, dict):
        for v in node.values():
            yield from _all_numbers(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _all_numbers(v)
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        yield node


def test_cross_sub_has_all_required_subobjects(fixture_duckdb):
    with open_duckdb(str(fixture_duckdb)) as reader:
        signal = reader.cross_subscription_reference_counts()
    xsub = cross_sub.extract(signal)
    assert set(xsub.keys()) == REQUIRED_XSUB_KEYS
    assert set(xsub["hub_spoke"].keys()) == {"probability", "hub_count"}
    assert set(xsub["hub_spoke"]["hub_count"].keys()) == {"mean", "std"}
    assert set(xsub["private_endpoints"].keys()) == {
        "probability",
        "per_spoke_count",
    }
    assert set(xsub["private_endpoints"]["per_spoke_count"].keys()) == {
        "mean",
        "std",
    }


def test_cross_sub_probabilities_in_unit_interval(fixture_duckdb):
    with open_duckdb(str(fixture_duckdb)) as reader:
        signal = reader.cross_subscription_reference_counts()
    xsub = cross_sub.extract(signal)
    for sub in xsub.values():
        assert 0.0 <= sub["probability"] <= 1.0


def test_cross_sub_all_numbers_finite(fixture_duckdb):
    """Every emitted probability/mean/std is finite -- no NaN, no None."""
    with open_duckdb(str(fixture_duckdb)) as reader:
        signal = reader.cross_subscription_reference_counts()
    xsub = cross_sub.extract(signal)
    nums = list(_all_numbers(xsub))
    assert nums
    assert all(math.isfinite(n) for n in nums)


def test_cross_sub_defaults_when_no_signal():
    """No cross-sub signal -> documented conservative defaults; hub_count
    {mean:2, std:1} and per_spoke_count {mean:3, std:2}."""
    xsub = cross_sub.extract(None)
    assert xsub["hub_spoke"]["probability"] == pytest.approx(0.70)
    assert xsub["hub_spoke"]["hub_count"] == {"mean": 2.0, "std": 1.0}
    assert xsub["shared_keyvault"]["probability"] == pytest.approx(0.50)
    assert xsub["centralized_logging"]["probability"] == pytest.approx(0.60)
    assert xsub["shared_acr"]["probability"] == pytest.approx(0.30)
    assert xsub["private_endpoints"]["probability"] == pytest.approx(0.40)
    assert xsub["private_endpoints"]["per_spoke_count"] == {
        "mean": 3.0,
        "std": 2.0,
    }
    assert all(math.isfinite(n) for n in _all_numbers(xsub))


def test_cross_sub_empty_signal_dict_falls_back_to_defaults():
    """A zero-signal dict (table present but no cross-refs) -> defaults."""
    xsub = cross_sub.extract(
        {
            "cross_ref_resources": 0,
            "spoke_subscriptions": 0,
            "hub_subscriptions": 0,
            "total_resources": 100,
        }
    )
    assert xsub["hub_spoke"]["hub_count"] == {"mean": 2.0, "std": 1.0}
    assert xsub["private_endpoints"]["per_spoke_count"] == {
        "mean": 3.0,
        "std": 2.0,
    }


def test_cross_sub_single_observation_std_is_zero_not_nan():
    """A single spoke/hub observation yields std 0.0, never NaN."""
    xsub = cross_sub.extract(
        {
            "cross_ref_resources": 3,
            "spoke_subscriptions": 1,
            "hub_subscriptions": 1,
            "total_resources": 50,
        }
    )
    assert xsub["hub_spoke"]["hub_count"]["std"] == 0.0
    assert xsub["private_endpoints"]["per_spoke_count"]["std"] == 0.0
    assert all(math.isfinite(n) for n in _all_numbers(xsub))


# --------------------------------------------------------------------------- #
# violations extractor
# --------------------------------------------------------------------------- #


def test_violation_type_frequencies_within_vocabulary(fixture_duckdb):
    with open_duckdb(str(fixture_duckdb)) as reader:
        finding_counts = reader.finding_type_counts()
        total = reader.total_resources()
    freqs = violations.extract(finding_counts, total, min_bucket_size=MIN_BUCKET_SIZE)
    assert freqs, "fixture has above-threshold mapped findings"
    assert set(freqs.keys()) <= violations.KNOWN_VIOLATION_VOCABULARY
    assert all(0.0 <= v <= 1.0 for v in freqs.values())


def test_violation_below_threshold_and_unmapped_dropped(fixture_duckdb):
    """unattached_disks (x2 < 5) and mystery_detector (unmapped) are dropped;
    unattached_nics (x8) maps to VM_NO_BACKUP and survives."""
    with open_duckdb(str(fixture_duckdb)) as reader:
        finding_counts = reader.finding_type_counts()
        total = reader.total_resources()
    freqs = violations.extract(finding_counts, total, min_bucket_size=MIN_BUCKET_SIZE)
    # unattached_nics -> VM_NO_BACKUP present.
    assert "VM_NO_BACKUP" in freqs
    # DISK_UNENCRYPTED comes only from unattached_disks (x2, dropped).
    assert "DISK_UNENCRYPTED" not in freqs


# --------------------------------------------------------------------------- #
# Full build_profile assembly
# --------------------------------------------------------------------------- #


def test_build_profile_fully_populated_and_schema_valid(fixture_duckdb, tmp_path):
    profile = build_profile(
        source=f"duckdb:{fixture_duckdb}",
        out=tmp_path / "derived.json",
        min_bucket_size=MIN_BUCKET_SIZE,
        allow_no_denylist=True,
    )
    # Schema-valid (raises otherwise).
    schema_validate.validate_profile(profile)

    # No placeholders left anywhere.
    assert all(
        a["id"] != "placeholder" for a in profile["subscription_archetypes"]
    )
    assert all(
        t["id"] != "placeholder" for t in profile["resource_group_templates"]
    )

    # tag_distributions populated from real signal.
    td = profile["tag_distributions"]
    assert td["key_frequencies"]
    assert td["value_distributions"]

    # cross_subscription_dependencies present, finite, all five sub-objects.
    xsub = profile["cross_subscription_dependencies"]
    assert set(xsub.keys()) == REQUIRED_XSUB_KEYS
    assert all(math.isfinite(n) for n in _all_numbers(xsub))

    # governance violations within the vocabulary.
    freqs = profile["governance_violations"]["type_frequencies"]
    assert set(freqs.keys()) <= violations.KNOWN_VIOLATION_VOCABULARY

    # At least one type carries real property/sku shapes (Task 1 merged here).
    has_shapes = any(
        v.get("property_distributions") or v.get("sku_distributions")
        for v in profile["resource_type_distributions"].values()
    )
    assert has_shapes


def test_no_duckdb_import_in_cross_sub_or_violations():
    for mod in (cross_sub, violations):
        assert "import duckdb" not in inspect.getsource(mod)
