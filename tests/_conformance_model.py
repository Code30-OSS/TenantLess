"""Pure (IO-free) pieces of the live ARM conformance machine.

* :class:`Shadow` — a THIN prediction helper keyed by the canonical identity key. It holds
  the logical state only (``{key: {id, present, source}}``) plus a ``ledger_continuous``
  flag. It deliberately carries NO revision math and NO ETag derivation: revisions and
  ETags are read from the real system and checked relatively, never recomputed here.
* The global-invariant predicates, each returning a list of human-readable violations
  (empty = holds) so a failing step can report every broken invariant at once.
* :data:`OP_REGISTRY` — the extensible op/transition registry. Every state-mutating
  feature adds its op here AND a matching rule on the machine as part of its own
  definition of done; a registry/rule parity test keeps the two from drifting apart.

Diagnostics are value-safe: ids, status codes and categories only, never request bodies
or credentials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from tenantless.identity import arm_id_key

# --------------------------------------------------------------------------------------- #
# Op registry
# --------------------------------------------------------------------------------------- #

# How an op relates to the drift ledger's parent -> result fingerprint chain:
#   "mint"  — a successful drift apply records a fingerprint and may extend the chain
#   "break" — the op changes resolved state WITHOUT minting a fingerprint (ARM writes never
#             touch the ledger; a revert recomputes state but records no fingerprint), so
#             the next apply's parent is not expected to equal the previous result
#   "none"  — the op must not change state at all (e.g. a stale-precondition write)
LEDGER_MINT = "mint"
LEDGER_BREAK = "break"
LEDGER_NONE = "none"


@dataclass(frozen=True)
class OpSpec:
    """One registered op: its category and its effect on the ledger chain."""

    name: str
    category: str  # "arm" (real HTTP server) | "drift" (real Python CLI)
    ledger_effect: str


# The CURRENT (pre-resource-group) op surface only. New ops — resource-group CRUD,
# container cascade, parallel schedules — are added here with their machine rule and
# shadow transition by the feature that introduces them, never speculatively.
OP_REGISTRY: dict[str, OpSpec] = {
    spec.name: spec
    for spec in (
        OpSpec("put_new", "arm", LEDGER_BREAK),
        OpSpec("put_existing", "arm", LEDGER_BREAK),
        OpSpec("put_resurrect", "arm", LEDGER_BREAK),
        OpSpec("patch", "arm", LEDGER_BREAK),
        OpSpec("patch_if_match_fresh", "arm", LEDGER_BREAK),
        OpSpec("delete", "arm", LEDGER_BREAK),
        OpSpec("stale_precondition_write", "arm", LEDGER_NONE),
        OpSpec("drift_apply", "drift", LEDGER_MINT),
        OpSpec("drift_appear", "drift", LEDGER_MINT),
        OpSpec("drift_disappear", "drift", LEDGER_MINT),
        OpSpec("drift_revert", "drift", LEDGER_BREAK),
    )
}


# --------------------------------------------------------------------------------------- #
# Identity helpers
# --------------------------------------------------------------------------------------- #


def segments(rid: str) -> list[str]:
    """Identity-folded path segments of an ARM id (empty segments dropped).

    Mirrors the server's segment parser: the canonical ASCII-only fold per segment, so
    containment agrees with the database identity key.
    """
    return [arm_id_key(s) for s in rid.split("/") if s]


def is_descendant(target: list[str], candidate: list[str]) -> bool:
    """Strict segment-granularity containment (never a raw string prefix).

    ``servers/s1`` does not contain ``servers/s10``; a resource is never its own
    descendant.
    """
    return len(candidate) > len(target) and candidate[: len(target)] == target


# --------------------------------------------------------------------------------------- #
# Shadow model
# --------------------------------------------------------------------------------------- #


@dataclass
class Shadow:
    """Logical state per identity key + the ledger-continuity flag. Nothing else."""

    entries: dict[str, dict] = field(default_factory=dict)
    ledger_continuous: bool = False
    last_result_fp: str | None = None
    last_scope: tuple | None = None

    @classmethod
    def from_db(cls, resolved_ids: list[str], overlay: dict[str, dict]) -> Shadow:
        """Build from the resolved view ids + the overlay rows (``{key: {...}}``)."""
        shadow = cls()
        shadow.refresh(resolved_ids, overlay)
        return shadow

    def refresh(self, resolved_ids: list[str], overlay: dict[str, dict]) -> None:
        """Adopt the database's view of logical state (used after drift ops, whose exact
        mutations the thin shadow does not predict)."""
        entries: dict[str, dict] = {}
        for rid in resolved_ids:
            key = arm_id_key(rid)
            src = overlay.get(key, {}).get("source", "baseline")
            entries[key] = {"id": rid, "present": True, "source": src}
        for key, row in overlay.items():
            if not row["present"]:
                entries[key] = {"id": row["id"], "present": False, "source": row["source"]}
        self.entries = entries

    def present_keys(self) -> list[str]:
        return sorted(k for k, e in self.entries.items() if e["present"])

    def tombstoned_keys(self) -> list[str]:
        return sorted(k for k, e in self.entries.items() if not e["present"])

    def user_keys(self) -> list[str]:
        return sorted(k for k, e in self.entries.items() if e["source"] == "user")

    def is_present(self, rid: str) -> bool:
        entry = self.entries.get(arm_id_key(rid))
        return bool(entry and entry["present"])

    def raw_id(self, rid: str) -> str | None:
        entry = self.entries.get(arm_id_key(rid))
        return entry["id"] if entry else None

    # ---- transitions ------------------------------------------------------------------ #

    def put(self, served_id: str) -> None:
        self.entries[arm_id_key(served_id)] = {
            "id": served_id,
            "present": True,
            "source": "user",
        }

    def patch(self, rid: str) -> None:
        self.entries[arm_id_key(rid)]["source"] = "user"

    def delete(self, rid: str) -> list[str]:
        """Tombstone ``rid`` and every PRESENT segment-descendant; return cascaded keys."""
        target = segments(rid)
        cascaded = [
            k
            for k, e in self.entries.items()
            if e["present"] and is_descendant(target, segments(e["id"]))
        ]
        key = arm_id_key(rid)
        prior = self.entries.get(key)
        self.entries[key] = {
            "id": prior["id"] if prior else rid,
            "present": False,
            "source": "user",
        }
        for k in cascaded:
            self.entries[k] = {**self.entries[k], "present": False, "source": "user"}
        return cascaded

    def note_ledger(
        self, ledger_effect: str, result_fp: str | None = None, scope: tuple | None = None
    ) -> None:
        """Advance the ledger-continuity flag for an op that SUCCEEDED.

        ``scope`` is the drift apply's ``(subscription, resource_types)`` filter: a batch
        fingerprints only its scope, so a chain is only defined between same-scope applies.
        """
        if ledger_effect == LEDGER_MINT:
            self.ledger_continuous = True
            self.last_result_fp = result_fp
            self.last_scope = scope
        elif ledger_effect == LEDGER_BREAK:
            self.ledger_continuous = False
            self.last_result_fp = None
            self.last_scope = None

    def chain_expected(self, scope: tuple | None) -> str | None:
        """The parent fingerprint the next apply over ``scope`` must carry, or ``None``
        when no chain is defined (an ARM write / revert intervened, or the previous apply
        fingerprinted a different scope)."""
        if self.ledger_continuous and self.last_scope == scope:
            return self.last_result_fp
        return None


# --------------------------------------------------------------------------------------- #
# Invariant predicates (each returns a list of violations; empty = holds)
# --------------------------------------------------------------------------------------- #


def duplicate_keys(resolved_ids: list[str]) -> list[str]:
    """Invariant 1: at most one logical resource per identity key."""
    seen: dict[str, int] = {}
    for rid in resolved_ids:
        k = arm_id_key(rid)
        seen[k] = seen.get(k, 0) + 1
    return [f"identity key {k} resolves to {n} rows" for k, n in sorted(seen.items()) if n > 1]


def served_body(row: dict) -> dict:
    """The ARM body the server must serve for a resolved-view row (8-column projection:
    ``sku``/``kind`` omitted when NULL, ``properties`` never null)."""
    body = {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "location": row["location"],
        "tags": row["tags"],
        "properties": row["properties"] if row["properties"] is not None else {},
    }
    if row.get("sku") is not None:
        body["sku"] = row["sku"]
    if row.get("kind") is not None:
        body["kind"] = row["kind"]
    return body


def _normalize(body: dict) -> str:
    # The server serves the canonical type casing, which may differ from the stored
    # half-canonical casing; the type is compared case-insensitively for that reason only.
    b = dict(body)
    if isinstance(b.get("type"), str):
        b["type"] = b["type"].lower()
    return json.dumps(b, sort_keys=True, separators=(",", ":"))


def served_mismatches(http_by_key: dict[str, dict], db_rows: list[dict]) -> list[str]:
    """Invariant 2: HTTP list agrees with the resolved DB state (same set + same bodies)."""
    out: list[str] = []
    db_by_key = {arm_id_key(r["id"]): served_body(r) for r in db_rows}
    for k in sorted(set(db_by_key) - set(http_by_key)):
        out.append(f"resolved row {k} missing from the HTTP list")
    for k in sorted(set(http_by_key) - set(db_by_key)):
        out.append(f"HTTP list serves {k} which the resolved view does not hold")
    for k in sorted(set(db_by_key) & set(http_by_key)):
        if _normalize(http_by_key[k]) != _normalize(db_by_key[k]):
            fields = sorted(
                f
                for f in set(http_by_key[k]) | set(db_by_key[k])
                if _normalize({f: http_by_key[k].get(f)}) != _normalize({f: db_by_key[k].get(f)})
            )
            out.append(f"served body of {k} differs from the resolved row in {fields}")
    return out


def shadow_mismatches(shadow: Shadow, resolved_ids: list[str]) -> list[str]:
    """Invariant 2 (shadow leg): the predicted present set equals the resolved set."""
    db_keys = {arm_id_key(r) for r in resolved_ids}
    sh_keys = set(shadow.present_keys())
    out = [f"shadow predicts {k} present; resolved view lacks it" for k in sorted(sh_keys - db_keys)]
    out += [f"resolved view holds {k}; shadow predicts absent" for k in sorted(db_keys - sh_keys)]
    return out


def user_row_violations(before: dict[str, dict], after: dict[str, dict]) -> list[str]:
    """Invariant 3: a drift op never modifies, removes or resurrects a user-owned row.

    ``before``/``after`` are overlay snapshots ``{key: {present, source, revision, body}}``.
    """
    out: list[str] = []
    for k, row in sorted(before.items()):
        if row["source"] != "user":
            continue
        now = after.get(k)
        if now is None:
            out.append(f"user-owned overlay row {k} was removed")
        elif now != row:
            changed = sorted(f for f in row if row[f] != now.get(f))
            out.append(f"user-owned overlay row {k} was modified ({changed})")
    return out


def revision_violations(
    before: dict[str, dict], after: dict[str, dict], seq_before: int, rev0: int
) -> list[str]:
    """Invariant 7 (and the no-change half of 6): every overlay row an op rewrote carries a
    revision strictly above the pre-op sequence value; untouched rows keep theirs; and every
    overlay row post-dates the example start (relative ordering only, gaps tolerated)."""
    out: list[str] = []
    for k, row in sorted(after.items()):
        prior = before.get(k)
        if prior is not None and prior["revision"] == row["revision"]:
            if prior != row:
                out.append(f"overlay row {k} changed content without a new revision")
        elif row["revision"] <= seq_before:
            out.append(
                f"overlay row {k} rewritten with a revision not above the pre-op sequence"
            )
        if row["revision"] <= rev0:
            out.append(f"overlay row {k} carries a revision from before the example start")
    return out


def cascade_violations(target_id: str, pre_ids: list[str], post_ids: list[str]) -> list[str]:
    """Invariant 8: no descendant that existed before the delete survives it."""
    target = segments(target_id)
    post_keys = {arm_id_key(r) for r in post_ids}
    out = [
        f"descendant {arm_id_key(r)} survived the delete of {arm_id_key(target_id)}"
        for r in pre_ids
        if is_descendant(target, segments(r)) and arm_id_key(r) in post_keys
    ]
    out += [
        f"resolved view holds {arm_id_key(r)} under deleted {arm_id_key(target_id)}"
        for r in post_ids
        if is_descendant(target, segments(r))
    ]
    return sorted(set(out))


def format_trace(trace: list[str]) -> str:
    """A readable, numbered op trace for failure reports (ids/status/category only)."""
    if not trace:
        return "  (no ops)"
    return "\n".join(f"  {i:>3}. {line}" for i, line in enumerate(trace, 1))


def violation_report(title: str, violations: list[str], trace: list[str]) -> str:
    """The failure message for a broken invariant: what broke, then the op trace.

    Hypothesis prints the generated step sequence and (``print_blob``) a
    ``@reproduce_failure`` blob alongside this; the run is also replayable with
    ``pytest --hypothesis-seed=<seed>``.
    """
    lines = [f"conformance invariant violated after: {title}"]
    lines += [f"  - {v}" for v in violations]
    lines.append("op trace (this example, baseline-restored start):")
    lines.append(format_trace(trace))
    lines.append(
        "replay: rerun with the printed @reproduce_failure blob or "
        "`pytest -m conformance --hypothesis-seed=<seed>`"
    )
    return "\n".join(lines)
