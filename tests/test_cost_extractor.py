"""Unit tests for the per-type lognormal cost extractor (COST-01 fit math).

These tests exercise ``extract_cost_distributions`` over a hand-built
``(type, monthly_cost)`` frame with KNOWN values, so the fitted ``mu``/``sigma``
can be checked against the closed-form lognormal MLE (``floc=0``):

    sigma = std(log(x))   (population, ddof=0)
    mu    = mean(log(x))

and the min-bucket privacy floor (a type with < min_bucket_size samples is
dropped) is provable. No DB / fixture is touched.
"""

from __future__ import annotations

import math

import numpy as np
import polars as pl
import pytest

from tenantless.analyzer.extractors.cost import extract_cost_distributions

VM_TYPE = "microsoft.compute/virtualmachines"
VM_CANONICAL = "Microsoft.compute/virtualmachines"
SA_TYPE = "microsoft.storage/storageaccounts"
SA_CANONICAL = "Microsoft.storage/storageaccounts"


def test_fits_lognormal_mle_per_type():
    """A >=5-sample type yields lognormal mu/sigma == closed-form MLE."""
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    logs = np.log(vals)
    expected_mu = float(logs.mean())
    expected_sigma = float(logs.std())  # population std (MLE, ddof=0)

    frame = pl.DataFrame({"type": [VM_TYPE] * 5, "monthly_cost": vals})
    out = extract_cost_distributions(frame, min_bucket_size=5)

    assert VM_CANONICAL in out
    entry = out[VM_CANONICAL]
    assert entry["distribution"] == "lognormal"
    assert entry["sample_count"] == 5
    assert entry["mu"] == pytest.approx(expected_mu, rel=1e-9)
    assert entry["sigma"] == pytest.approx(expected_sigma, rel=1e-6)


def test_canonicalizes_type_key():
    """Lowercase seed type strings emerge as canonical Microsoft.* keys."""
    frame = pl.DataFrame(
        {"type": [VM_TYPE] * 5, "monthly_cost": [1.0, 2.0, 3.0, 4.0, 5.0]}
    )
    out = extract_cost_distributions(frame, min_bucket_size=5)
    assert VM_CANONICAL in out
    assert VM_TYPE not in out  # the raw lowercase key never survives


def test_drops_type_below_min_bucket():
    """A type with 4 samples is dropped; a >=5-sample type survives."""
    frame = pl.DataFrame(
        {
            "type": [VM_TYPE] * 5 + [SA_TYPE] * 4,
            "monthly_cost": [10.0, 20.0, 30.0, 40.0, 50.0] + [1.0, 2.0, 3.0, 4.0],
        }
    )
    out = extract_cost_distributions(frame, min_bucket_size=5)

    assert VM_CANONICAL in out  # 5 samples -> kept
    assert SA_CANONICAL not in out  # 4 samples -> dropped by the privacy floor


def test_empty_frame_yields_empty_dict():
    """An empty cost frame yields an empty dict (cost-less source back-compat)."""
    frame = pl.DataFrame(
        {"type": [], "monthly_cost": []},
        schema={"type": pl.Utf8, "monthly_cost": pl.Float64},
    )
    assert extract_cost_distributions(frame, min_bucket_size=5) == {}


def test_emits_only_numeric_params_and_string_literals():
    """Every emitted value is a number or a known literal -- no stray strings."""
    frame = pl.DataFrame(
        {"type": [VM_TYPE] * 6, "monthly_cost": [5.0, 6.0, 7.0, 8.0, 9.0, 10.0]}
    )
    out = extract_cost_distributions(frame, min_bucket_size=5)
    entry = out[VM_CANONICAL]
    assert set(entry.keys()) == {"distribution", "mu", "sigma", "sample_count"}
    assert entry["distribution"] == "lognormal"
    assert isinstance(entry["mu"], float) and math.isfinite(entry["mu"])
    assert isinstance(entry["sigma"], float) and math.isfinite(entry["sigma"])
    assert isinstance(entry["sample_count"], int)
