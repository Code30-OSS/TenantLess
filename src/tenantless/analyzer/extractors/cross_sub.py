"""Cross-subscription dependency extractor (source-agnostic).

Derives the ``cross_subscription_dependencies`` profile fragment from a small
cross-reference signal dict (produced by ``reader.cross_subscription_reference_counts``):
how many resources reference a resource id in a DIFFERENT subscription, how many
subscriptions originate such references (spokes), and how many distinct target
subscriptions are referenced (hubs).

Output shape (profiles/schema.json):

    {
      "hub_spoke":           {"probability": 0..1, "hub_count": {mean, std}},
      "shared_keyvault":     {"probability": 0..1},
      "centralized_logging": {"probability": 0..1},
      "shared_acr":          {"probability": 0..1},
      "private_endpoints":   {"probability": 0..1, "per_spoke_count": {mean, std}},
    }

Conservative DEFAULTS (mirroring profiles/test-small.json) are emitted for any
sub-object whose real signal is weak/absent. These are the SAME values as the
hand-authored test-small.json so the derived profile stays drop-in compatible:

    hub_spoke:           probability 0.70, hub_count {mean: 2, std: 1}
    shared_keyvault:     probability 0.50
    centralized_logging: probability 0.60
    shared_acr:          probability 0.30
    private_endpoints:   probability 0.40, per_spoke_count {mean: 3, std: 2}

Heuristic -> dependency-kind mapping (documented, intentionally conservative):
    * hub_spoke: a cross-sub reference signal (>=1 spoke pointing at a hub) means
      a hub-spoke topology exists; probability = share of subscriptions that are
      spokes, falling back to the default 0.70 when there is no signal. hub_count
      = the number of distinct hub subscriptions (mean), std 0.0 for a single
      observation (never NaN), falling back to {mean: 2, std: 1} with no signal.
    * private_endpoints: cross-sub references are predominantly private-endpoint
      style in real Azure tenants; probability = share of resources that are
      cross-sub references, falling back to 0.40 with no signal. per_spoke_count
      = average cross-sub references per spoke subscription (mean), std 0.0 for a
      single spoke, falling back to {mean: 3, std: 2} with no signal.
    * shared_keyvault / centralized_logging / shared_acr: the current scan offers
      no robust, low-false-positive signal to distinguish these specific shared
      services from a generic text scan, so they emit the documented conservative
      defaults (0.50 / 0.60 / 0.30). Phase 6 can refine with provider-typed joins.

INVARIANT: every emitted probability / mean / std is a FINITE float -- never
NaN, never None. ``std`` is 0.0 (not NaN) when only one observation is available.

Source-agnostic: imports neither ``duckdb`` nor any reader type.
"""

from __future__ import annotations

import math
from typing import Any

# Conservative defaults copied verbatim from profiles/test-small.json.
DEFAULT_HUB_SPOKE = {"probability": 0.70, "hub_count": {"mean": 2.0, "std": 1.0}}
DEFAULT_SHARED_KEYVAULT = {"probability": 0.50}
DEFAULT_CENTRALIZED_LOGGING = {"probability": 0.60}
DEFAULT_SHARED_ACR = {"probability": 0.30}
DEFAULT_PRIVATE_ENDPOINTS = {
    "probability": 0.40,
    "per_spoke_count": {"mean": 3.0, "std": 2.0},
}


def _finite(value: float, default: float) -> float:
    """Return ``value`` if finite, else ``default`` (guards NaN/inf/None)."""
    if value is None:
        return float(default)
    f = float(value)
    if not math.isfinite(f):
        return float(default)
    return f


def _clamp_prob(value: float) -> float:
    """Clamp a probability into [0, 1]."""
    return max(0.0, min(1.0, float(value)))


def extract(signal: dict[str, int] | None) -> dict[str, Any]:
    """Build ``cross_subscription_dependencies`` from a cross-ref signal dict.

    Parameters
    ----------
    signal:
        ``{"cross_ref_resources", "spoke_subscriptions", "hub_subscriptions",
        "total_resources"}`` from ``reader.cross_subscription_reference_counts``.
        ``None`` / empty yields the all-defaults profile.

    Returns a fully-populated, finite-valued cross_subscription_dependencies dict.
    """
    if not signal:
        return _all_defaults()

    cross_refs = int(signal.get("cross_ref_resources", 0) or 0)
    spokes = int(signal.get("spoke_subscriptions", 0) or 0)
    hubs = int(signal.get("hub_subscriptions", 0) or 0)
    total = int(signal.get("total_resources", 0) or 0)

    has_signal = cross_refs > 0 and spokes > 0

    # --- hub_spoke -------------------------------------------------------- #
    if has_signal and total > 0:
        # Probability proxy: spokes are a meaningful share of the tenant, but we
        # never let a tiny tenant inflate this past the conservative default's
        # spirit -- use the spoke share of all referencing resources as a bounded
        # signal, blended toward the observed topology presence.
        hub_prob = _clamp_prob(
            _finite(spokes / max(total, 1), DEFAULT_HUB_SPOKE["probability"])
        )
        # If a hub-spoke topology is observed at all, it is at least as likely as
        # the conservative floor; take the max so a real topology never reads as
        # less likely than the default assumption.
        hub_prob = max(hub_prob, DEFAULT_HUB_SPOKE["probability"])
        hub_count_mean = _finite(float(hubs), DEFAULT_HUB_SPOKE["hub_count"]["mean"])
        # Single hub observation -> std 0.0 (NOT NaN); >1 distinct hubs is still a
        # single aggregate count here, so std stays 0.0 deterministically.
        hub_count_std = 0.0
        hub_spoke = {
            "probability": hub_prob,
            "hub_count": {
                "mean": _finite(hub_count_mean, DEFAULT_HUB_SPOKE["hub_count"]["mean"]),
                "std": _finite(hub_count_std, 0.0),
            },
        }
    else:
        hub_spoke = _copy(DEFAULT_HUB_SPOKE)

    # --- private_endpoints ------------------------------------------------ #
    if has_signal and total > 0:
        pe_prob = _clamp_prob(
            _finite(cross_refs / max(total, 1), DEFAULT_PRIVATE_ENDPOINTS["probability"])
        )
        per_spoke_mean = _finite(
            cross_refs / max(spokes, 1),
            DEFAULT_PRIVATE_ENDPOINTS["per_spoke_count"]["mean"],
        )
        # Single spoke -> std 0.0 (never NaN); a finer per-spoke variance would
        # need per-spoke counts, which Phase 6 can supply.
        per_spoke_std = 0.0
        private_endpoints = {
            "probability": pe_prob,
            "per_spoke_count": {
                "mean": _finite(
                    per_spoke_mean,
                    DEFAULT_PRIVATE_ENDPOINTS["per_spoke_count"]["mean"],
                ),
                "std": _finite(per_spoke_std, 0.0),
            },
        }
    else:
        private_endpoints = _copy(DEFAULT_PRIVATE_ENDPOINTS)

    # --- shared services: conservative defaults (no robust signal) -------- #
    result = {
        "hub_spoke": hub_spoke,
        "shared_keyvault": _copy(DEFAULT_SHARED_KEYVAULT),
        "centralized_logging": _copy(DEFAULT_CENTRALIZED_LOGGING),
        "shared_acr": _copy(DEFAULT_SHARED_ACR),
        "private_endpoints": private_endpoints,
    }
    _assert_finite(result)
    return result


def _all_defaults() -> dict[str, Any]:
    result = {
        "hub_spoke": _copy(DEFAULT_HUB_SPOKE),
        "shared_keyvault": _copy(DEFAULT_SHARED_KEYVAULT),
        "centralized_logging": _copy(DEFAULT_CENTRALIZED_LOGGING),
        "shared_acr": _copy(DEFAULT_SHARED_ACR),
        "private_endpoints": _copy(DEFAULT_PRIVATE_ENDPOINTS),
    }
    _assert_finite(result)
    return result


def _copy(d: dict[str, Any]) -> dict[str, Any]:
    """Deep-ish copy of a small defaults dict (one level of nesting)."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        out[k] = dict(v) if isinstance(v, dict) else v
    return out


def _assert_finite(node: Any) -> None:
    """Defensive: raise if any numeric in the tree is non-finite (NaN/inf/None)."""
    if isinstance(node, dict):
        for v in node.values():
            _assert_finite(v)
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        if not math.isfinite(float(node)):
            raise ValueError(
                "cross_subscription_dependencies emitted a non-finite number"
            )
