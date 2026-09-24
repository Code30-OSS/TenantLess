"""Live ARM state-model conformance: a Hypothesis ``RuleBasedStateMachine`` that drives the
REAL system and asserts global invariants after every operation.

Given a known baseline tenant in a dedicated PG16 database, served by the real Rust server,
When a generated sequence of operations runs — ARM ``PUT``/``PATCH``/``DELETE`` over HTTP
against the real server, and drift apply / appear / disappear / revert through the real
Python CLI on the same database —
Then after EVERY operation the HTTP detail/list responses, the resolved database view
(``synthetic.arm_resolved_resources``, queried directly) and a small shadow model agree,
and these global invariants hold:

1. At most one logical resource per canonical identity key in the resolved view.
2. HTTP detail/list agree with the resolved DB state (same set + same served bodies).
3. Drift never modifies, removes or resurrects a user-owned (``source='user'``) row.
4. Immediately after a SUCCESSFUL drift apply, the latest batch's ``result_fingerprint``
   equals the fingerprint of the persisted resolved state (and its parent equals the
   state it was applied to). ARM writes never touch the ledger and a revert records no
   fingerprint, so neither is checked against it.
5. Consecutive drift batches chain (``parent == previous result``) ONLY across consecutive
   applies with no intervening successful ARM write or revert.
6. A failed / stale write (``412``, ``404``) leaves state AND revisions unchanged.
7. Successful mutations advance revisions monotonically, relative to the example start
   (gaps tolerated, absolute values never asserted).
8. A delete leaves no descendant that existed before it.

The baseline is restored before every example with the real ``reset`` command (overlay +
drift ledger cleared; the revision sequence is never rewound). A failing example prints
the Hypothesis reproducer, the generated step sequence and a numbered op trace (ids,
status codes and categories only — never bodies or credentials).

The live machine is marked ``conformance`` (deselected by default; its own CI job runs it
under ``TENANTLESS_REQUIRE_LIVE=1`` where missing infrastructure FAILS instead of skipping).
The pure model/predicate tests below it run in the default suite.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from _conformance_model import (
    LEDGER_BREAK,
    LEDGER_MINT,
    LEDGER_NONE,
    OP_REGISTRY,
    Shadow,
    cascade_violations,
    duplicate_keys,
    format_trace,
    is_descendant,
    revision_violations,
    segments,
    served_body,
    served_mismatches,
    shadow_mismatches,
    user_row_violations,
    violation_report,
)
from conftest import reset_conformance_baseline, run_cli_inprocess
from tenantless.identity import arm_id_key

# --------------------------------------------------------------------------------------- #
# Run size (env-tunable so CI can raise it without a code change)
# --------------------------------------------------------------------------------------- #

_EXAMPLES = int(os.environ.get("TENANTLESS_CONFORMANCE_EXAMPLES", "25"))
_STEPS = int(os.environ.get("TENANTLESS_CONFORMANCE_STEPS", "20"))

CONFORMANCE_SETTINGS = settings(
    # Real HTTP + DB per step: the default per-example deadline would flake.
    deadline=None,
    max_examples=_EXAMPLES,
    stateful_step_count=_STEPS,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    print_blob=True,
)

_BATCH_RE = re.compile(r"apply-drift batch ([0-9a-fA-F-]{36})")
_ETAG_RE = re.compile(r'^"o-(\d+)"$')

# Small name pools so generated ops collide on purpose: casing variants of the same id,
# the servers/cf1 vs servers/cf10 sibling-lookalike cascade trap, re-create after delete.
_NAMES = st.sampled_from(["cf1", "cf10", "cf2"])
_CHILDREN = st.sampled_from(["db1", "db10"])
_CASINGS = st.sampled_from(["as-is", "upper", "mixed"])
_TAGS = st.sampled_from(["a", "b", "c"])
# "nested": a new child under an existing live resource, in the drawn casing — the
# case-variant parent/child seam drift's leaf rule must see through.
_SHAPES = st.sampled_from(["storage", "server", "database", "nested"])
_PICK = st.integers(min_value=0, max_value=10_000)

# Session-wide run statistics for the non-vacuity floor + the run report.
STATS: dict = {"examples": 0, "steps": 0, "ops": {}, "cascades": 0, "chain_checks": 0,
               "scoped_applies": {}, "resurrections": 0, "resurrect_casing_checks": 0}


# --------------------------------------------------------------------------------------- #
# Harness: the real system's IO surface (HTTP server + resolved-view oracle + CLI)
# --------------------------------------------------------------------------------------- #


def _recase(text: str, casing: str) -> str:
    if casing == "upper":
        return text.upper()
    if casing == "mixed":
        return "".join(c.upper() if i % 2 else c.lower() for i, c in enumerate(text))
    return text


def split_id(rid: str) -> tuple[str, str, str]:
    """``(subscription, resource_group, provider_tail)`` of a resource id (the literal
    route segments are matched case-insensitively)."""
    parts = rid.strip("/").split("/")
    assert parts[0].lower() == "subscriptions" and parts[2].lower() == "resourcegroups"
    assert parts[4].lower() == "providers", f"not a provider resource id: {rid}"
    return parts[1], parts[3], "/".join(parts[5:])


def route_id(sub: str, rg: str, tail: str) -> str:
    return f"/subscriptions/{sub}/resourceGroups/{rg}/providers/{tail}"


@dataclass
class Harness:
    url: str  # conformance database URL (never logged)
    base_url: str  # real server base URL
    pg: object  # autocommit psycopg connection
    scopes: list  # [(subscription_id, rg_name)] from the baseline
    subscriptions: list
    types: list  # stored resource types, for type-filtered drift scopes

    # ---- HTTP (real server) --------------------------------------------------------- #

    def http(self, method: str, path: str, body=None, headers=None):
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer conformance")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                status, hdrs = resp.status, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            raw, status, hdrs = exc.read(), exc.code, dict(exc.headers)
        parsed = json.loads(raw) if raw else None
        return status, hdrs, parsed

    def http_list(self) -> dict[str, dict]:
        """Every resource served by the per-subscription ARM list, keyed by identity."""
        out: dict[str, dict] = {}
        for sub in self.subscriptions:
            url = f"/subscriptions/{sub}/resources?$top=100"
            pages = 0
            while url:
                status, _h, body = self.http("GET", url)
                assert status == 200, f"list {sub} returned {status}"
                for item in body["value"]:
                    key = arm_id_key(item["id"])
                    assert key not in out, f"HTTP list serves {key} twice"
                    out[key] = item
                url = body.get("nextLink")
                pages += 1
                assert pages < 10_000, "runaway nextLink"
        return out

    # ---- resolved-view oracle (direct DB reads) ------------------------------------- #

    def resolved_rows(self) -> list[dict]:
        with self.pg.cursor() as cur:
            cur.execute(
                "SELECT id, name, type, location, tags, sku, kind, properties "
                "FROM synthetic.arm_resolved_resources"
            )
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def resolved_ids(self) -> list[str]:
        with self.pg.cursor() as cur:
            cur.execute("SELECT id FROM synthetic.arm_resolved_resources")
            return [r[0] for r in cur.fetchall()]

    def db_duplicate_keys(self) -> list:
        with self.pg.cursor() as cur:
            cur.execute(
                "SELECT synthetic.arm_id_key(id), count(*) "
                "FROM synthetic.arm_resolved_resources GROUP BY 1 HAVING count(*) > 1"
            )
            return cur.fetchall()

    def overlay(self) -> dict[str, dict]:
        with self.pg.cursor() as cur:
            cur.execute(
                "SELECT id_lower, id, present, source, revision, body::text "
                "FROM synthetic.arm_overlay WHERE target_kind = 'resource'"
            )
            return {
                k: {"id": i, "present": p, "source": s, "revision": r, "body": b}
                for k, i, p, s, r, b in cur.fetchall()
            }

    def seq(self) -> int:
        """The highest revision the sequence has issued (0 if never called)."""
        with self.pg.cursor() as cur:
            cur.execute(
                "SELECT last_value, is_called FROM synthetic.arm_overlay_revision_seq"
            )
            last, called = cur.fetchone()
        return last if called else last - 1

    def batches(self) -> list[dict]:
        with self.pg.cursor() as cur:
            cur.execute(
                "SELECT batch_id::text, parent_fingerprint, result_fingerprint, "
                "reverted_at IS NOT NULL FROM synthetic.drift_batches ORDER BY seq"
            )
            return [
                {"batch_id": b, "parent": p, "result": r, "reverted": rv}
                for b, p, r, rv in cur.fetchall()
            ]

    def state_fp(self, scope: tuple = (None, None)) -> str:
        """Fingerprint of the persisted resolved state over a drift scope
        ``(subscription, resource_types)`` — the same projection the ledger fingerprints."""
        from tenantless.cli import _build_scoped_read_sql, _fingerprint_row
        from tenantless.generator import drift

        sub, types = scope
        sql, params = _build_scoped_read_sql(sub, list(types) if types else None)
        with self.pg.cursor() as cur:
            cur.execute(sql, params)
            rows = [_fingerprint_row(r) for r in cur.fetchall()]
        return drift.state_fingerprint(rows)

    def snap(self) -> dict:
        return {"seq": self.seq(), "overlay": self.overlay(), "ids": self.resolved_ids()}

    # ---- id construction -------------------------------------------------------------- #

    def make_id(self, scope: int, shape: str, name: str, child: str, casing: str) -> str:
        sub, rg = self.scopes[scope % len(self.scopes)]
        tail = {
            "storage": f"Microsoft.Storage/storageAccounts/{name}",
            "server": f"Microsoft.Sql/servers/{name}",
            "database": f"Microsoft.Sql/servers/{name}/databases/{child}",
        }[shape]
        return route_id(sub, _recase(rg, casing), _recase(tail, casing))


def build_harness(url: str, base_url: str, pg) -> Harness:
    with pg.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT subscription_id::text, resource_group_name "
            "FROM synthetic.resources ORDER BY 1, 2"  # SYNRES-ALLOW[test-oracle]: baseline scopes for generated ids
        )
        scopes = cur.fetchall()
        cur.execute("SELECT subscription_id::text FROM synthetic.subscriptions ORDER BY 1")
        subs = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT type FROM synthetic.arm_resolved_resources ORDER BY 1")
        types = [r[0] for r in cur.fetchall()]
    assert scopes and subs and types, "conformance baseline is empty"
    return Harness(url=url, base_url=base_url, pg=pg, scopes=scopes, subscriptions=subs,
                   types=types)


# --------------------------------------------------------------------------------------- #
# The machine
# --------------------------------------------------------------------------------------- #


class ArmConformance(RuleBasedStateMachine):
    """Real-system conformance machine. Rules = the ops in ``OP_REGISTRY`` (parity-tested)."""

    created = Bundle("created")
    # Highest revision observed across the whole session: a reset must never rewind it.
    last_seq_seen = 0

    def __init__(self, harness: Harness):
        super().__init__()
        self.h = harness
        reset_conformance_baseline(harness.url)
        self.trace: list[str] = []
        self.rev0 = self.h.seq()
        self._check(
            [] if self.rev0 >= ArmConformance.last_seq_seen else
            ["baseline reset rewound the revision sequence"],
            "baseline restore",
        )
        self._check(
            (["overlay not empty after reset"] if self.h.overlay() else [])
            + (["drift ledger not empty after reset"] if self.h.batches() else []),
            "baseline restore",
        )
        self.shadow = Shadow.from_db(self.h.resolved_ids(), {})
        self.active_batches: list[str] = []
        STATS["examples"] += 1

    # ---- reporting -------------------------------------------------------------------- #

    def _check(self, violations: list[str], title: str) -> None:
        if violations:
            raise AssertionError(violation_report(title, violations, self.trace))

    def _record(self, op: str, detail: str, ok: bool) -> None:
        self.trace.append(f"{OP_REGISTRY[op].category}:{op} {detail}")
        STATS["steps"] += 1
        if ok:
            STATS["ops"][op] = STATS["ops"].get(op, 0) + 1

    def _detail_checks(self, rids: list[str]) -> list[str]:
        """Invariant 2 (detail leg): GET detail matches the resolved row / 404s."""
        rows = {arm_id_key(r["id"]): r for r in self.h.resolved_rows()}
        out: list[str] = []
        for rid in rids:
            key = arm_id_key(rid)
            status, _h, body = self.h.http("GET", rid)
            if key in rows:
                if status != 200:
                    out.append(f"detail {key} returned {status}, resolved view holds it")
                else:
                    out += served_mismatches({key: body}, [rows[key]])
            elif status != 404:
                out.append(f"detail {key} returned {status}, resolved view lacks it")
        return out

    def _unchanged(self, before: dict, after: dict) -> list[str]:
        """Invariant 6: state AND revisions unchanged."""
        out = []
        if after["seq"] != before["seq"]:
            out.append("revision sequence advanced on a failed write")
        if after["overlay"] != before["overlay"]:
            out.append("overlay changed on a failed write")
        if sorted(after["ids"]) != sorted(before["ids"]):
            out.append("resolved set changed on a failed write")
        return out

    def _etag_revision(self, headers: dict) -> int | None:
        tok = headers.get("ETag") or headers.get("etag")
        m = _ETAG_RE.match(tok or "")
        return int(m.group(1)) if m else None

    def _successful_write_checks(self, before: dict, after: dict, headers: dict) -> list[str]:
        """Invariant 7 for an accepted ARM write."""
        out = revision_violations(before["overlay"], after["overlay"], before["seq"], self.rev0)
        rev = self._etag_revision(headers)
        if rev is None:
            out.append("accepted write carried no o-<revision> ETag")
        elif not before["seq"] < rev <= after["seq"]:
            out.append("write ETag revision is not above the pre-op sequence")
        if after["seq"] <= before["seq"]:
            out.append("accepted write did not advance the revision sequence")
        return out

    def _pick_present(self, pick: int) -> str:
        keys = self.shadow.present_keys()
        return self.shadow.entries[keys[pick % len(keys)]]["id"]

    # ---- ARM ops (real HTTP server) --------------------------------------------------- #

    def _put(self, op: str, rid: str, tag: str) -> str:
        before = self.h.snap()
        existed = self.shadow.is_present(rid)
        prior_id = self.shadow.raw_id(rid) if existed else None
        # A tombstone keeps its frozen stored id: resurrection through a differently-cased
        # route must serve that casing, never the route casing (D-25).
        entry = self.shadow.entries.get(arm_id_key(rid))
        frozen_id = entry["id"] if entry is not None and not entry["present"] else None
        status, headers, body = self.h.http(
            "PUT", rid, {"location": "eastus", "tags": {"cf": tag}, "properties": {"m": tag}}
        )
        expect = 200 if existed else 201
        self._record(op, f"id={arm_id_key(rid)} -> {status}", status == expect)
        self._check([] if status == expect else [f"PUT expected {expect}, got {status}"], op)
        served_id = body["id"]
        v = []
        if existed and served_id != prior_id:
            v.append("PUT over an existing id did not keep its stored casing")
        if frozen_id is not None:
            STATS["resurrections"] += 1
            if rid != frozen_id:
                STATS["resurrect_casing_checks"] += 1
            if served_id != frozen_id:
                v.append("PUT resurrecting a tombstone did not keep its frozen casing")
        if arm_id_key(served_id) != arm_id_key(rid):
            v.append("PUT served a different identity than the route")
        if body.get("tags") != {"cf": tag}:
            v.append("PUT echo does not carry the written tags")
        after = self.h.snap()
        v += self._successful_write_checks(before, after, headers)
        self.shadow.put(served_id)
        self.shadow.note_ledger(OP_REGISTRY[op].ledger_effect)
        v += self._detail_checks([rid])
        self._check(v, op)
        return rid

    @rule(target=created, scope=_PICK, shape=_SHAPES, name=_NAMES, child=_CHILDREN,
          casing=_CASINGS, tag=_TAGS)
    def put_new(self, scope, shape, name, child, casing, tag):
        if shape == "nested":
            if not self.shadow.present_keys():
                shape = "storage"
            else:
                sub, rg, tail = split_id(self._pick_present(scope))
                rid = route_id(sub, _recase(rg, casing),
                               _recase(f"{tail}/cfChildren/{name}", casing))
                return self._put("put_new", rid, tag)
        return self._put("put_new", self.h.make_id(scope, shape, name, child, casing), tag)

    @precondition(lambda self: self.shadow.present_keys())
    @rule(pick=_PICK, casing=_CASINGS, tag=_TAGS)
    def put_existing(self, pick, casing, tag):
        sub, rg, tail = split_id(self._pick_present(pick))
        self._put("put_existing", route_id(sub, _recase(rg, casing), _recase(tail, casing)), tag)

    @precondition(lambda self: self.shadow.tombstoned_keys())
    @rule(pick=_PICK, casing=_CASINGS, tag=_TAGS)
    def put_resurrect(self, pick, casing, tag):
        # PUT over a tombstoned identity, usually through a differently-cased route.
        keys = self.shadow.tombstoned_keys()
        sub, rg, tail = split_id(self.shadow.entries[keys[pick % len(keys)]]["id"])
        self._put("put_resurrect", route_id(sub, _recase(rg, casing), _recase(tail, casing)),
                  tag)

    @rule(rid=created, tag=_TAGS)
    def patch(self, rid, tag):
        before = self.h.snap()
        existed = self.shadow.is_present(rid)
        status, headers, body = self.h.http(
            "PATCH", rid, {"tags": {"cf": tag}, "properties": {"p": tag}}
        )
        expect = 200 if existed else 404
        self._record("patch", f"id={arm_id_key(rid)} -> {status}", status == expect)
        self._check([] if status == expect else [f"PATCH expected {expect}, got {status}"],
                    "patch")
        after = self.h.snap()
        if existed:
            v = self._successful_write_checks(before, after, headers)
            if body["id"] != self.shadow.raw_id(rid):
                v.append("PATCH did not keep the stored casing")
            self.shadow.patch(rid)
            self.shadow.note_ledger(OP_REGISTRY["patch"].ledger_effect)
        else:
            v = self._unchanged(before, after)  # a 404 PATCH never creates (inv 6)
        v += self._detail_checks([rid])
        self._check(v, "patch")

    @precondition(lambda self: self.shadow.present_keys())
    @rule(pick=_PICK, tag=_TAGS)
    def patch_if_match_fresh(self, pick, tag):
        rid = self._pick_present(pick)
        gs, gh, _gb = self.h.http("GET", rid)
        etag = gh.get("ETag") or gh.get("etag")
        self._check([] if gs == 200 and etag else [f"GET before If-Match returned {gs}"],
                    "patch_if_match_fresh")
        before = self.h.snap()
        status, headers, _body = self.h.http(
            "PATCH", rid, {"tags": {"cf": tag}}, headers={"If-Match": etag}
        )
        self._record("patch_if_match_fresh", f"id={arm_id_key(rid)} -> {status}",
                     status == 200)
        self._check([] if status == 200 else [f"fresh If-Match PATCH got {status}"],
                    "patch_if_match_fresh")
        after = self.h.snap()
        v = self._successful_write_checks(before, after, headers)
        self.shadow.patch(rid)
        self.shadow.note_ledger(OP_REGISTRY["patch_if_match_fresh"].ledger_effect)
        v += self._detail_checks([rid])
        self._check(v, "patch_if_match_fresh")

    @rule(pick=_PICK)
    def delete(self, pick):
        # User-owned ids first, so small picks favour the generated parent/child trees
        # (cascade coverage); every other known id (baseline, drift) follows.
        keys = self.shadow.user_keys() + [
            k for k in sorted(self.shadow.entries) if self.shadow.entries[k]["source"] != "user"
        ]
        rid = self.shadow.entries[keys[pick % len(keys)]]["id"]
        before = self.h.snap()
        status, headers, _body = self.h.http("DELETE", rid)
        self._record("delete", f"id={arm_id_key(rid)} -> {status}", status == 204)
        self._check([] if status == 204 else [f"DELETE expected 204, got {status}"], "delete")
        after = self.h.snap()
        v = self._successful_write_checks(before, after, headers)
        v += cascade_violations(rid, before["ids"], after["ids"])  # inv 8
        cascaded = self.shadow.delete(rid)
        STATS["cascades"] += 1 if cascaded else 0
        self.shadow.note_ledger(OP_REGISTRY["delete"].ledger_effect)
        v += self._detail_checks([rid] + [self.shadow.entries[k]["id"] for k in cascaded])
        self._check(v, "delete")

    @precondition(lambda self: self.shadow.present_keys())
    @rule(pick=_PICK, variant=st.sampled_from(
        ["put-if-match", "patch-if-match", "delete-if-match", "put-if-none-match"]))
    def stale_precondition_write(self, pick, variant):
        rid = self._pick_present(pick)
        method = variant.split("-", 1)[0].upper()
        headers = ({"If-None-Match": "*"} if variant == "put-if-none-match"
                   else {"If-Match": '"o-0"'})  # revisions are > 0: never current
        body = None if method == "DELETE" else {"location": "eastus", "tags": {"cf": "stale"}}
        before = self.h.snap()
        status, _h, _b = self.h.http(method, rid, body, headers=headers)
        self._record("stale_precondition_write", f"{variant} id={arm_id_key(rid)} -> {status}",
                     status == 412)
        v = [] if status == 412 else [f"stale precondition expected 412, got {status}"]
        v += self._unchanged(before, self.h.snap())  # inv 6
        self.shadow.note_ledger(OP_REGISTRY["stale_precondition_write"].ledger_effect)
        self._check(v, "stale_precondition_write")

    # ---- drift ops (real Python CLI, same database) ----------------------------------- #

    def _apply(self, op: str, args: list[str], expect: str,
               scope: tuple = (None, None)) -> None:
        sub, types = scope
        if sub is not None:
            args = [*args, "--subscription", sub]
        if types is not None:
            args = [*args, "--resource-types", ",".join(types)]
        before = self.h.snap()
        pre_types = ({arm_id_key(r["id"]): r["type"] for r in self.h.resolved_rows()}
                     if types is not None else {})
        pre_fp = self.h.state_fp(scope)
        expected_parent = self.shadow.chain_expected(scope)
        code, out = run_cli_inprocess("apply-drift", *args, "--database-url", self.h.url)
        m = _BATCH_RE.search(out)
        self._record(op, f"{' '.join(args)} -> exit {code}", code == 0 and m is not None)
        self._check([] if code == 0 and m else [f"apply-drift failed (exit {code}): {out[-600:]}"],
                    op)
        bid = m.group(1)
        after = self.h.snap()
        v = user_row_violations(before["overlay"], after["overlay"])  # inv 3
        v += revision_violations(before["overlay"], after["overlay"], before["seq"], self.rev0)
        if after["seq"] < before["seq"]:
            v.append("revision sequence went backwards")
        # inv 4: the ledger records the persisted state, immediately after the apply.
        latest = self.h.batches()[-1]
        if latest["batch_id"] != bid:
            v.append("the applied batch is not the latest ledger batch")
        if latest["result"] != self.h.state_fp(scope):
            v.append("result_fingerprint differs from the persisted resolved state")
        if latest["parent"] != pre_fp:
            v.append("parent_fingerprint differs from the state the apply read")
        # inv 5: chain only across consecutive same-scope applies with nothing in between.
        if expected_parent is not None:
            STATS["chain_checks"] += 1
            if latest["parent"] != expected_parent:
                v.append("consecutive drift batches do not chain (parent != previous result)")
        # Lifecycle predictions (thin): which keys may appear / disappear.
        pre = {arm_id_key(r) for r in before["ids"]}
        post = {arm_id_key(r) for r in after["ids"]}
        gone, new = pre - post, post - pre
        v += [f"drift removed user-owned {k}" for k in sorted(gone)
              if self.shadow.entries.get(k, {}).get("source") == "user"]
        v += [f"drift minted {k} over a known identity" for k in sorted(new)
              if k in self.shadow.entries]
        if expect in ("fields", "appear") and gone:
            v.append(f"{op} removed resources it must not remove: {len(gone)}")
        if expect in ("fields", "disappear") and new:
            v.append(f"{op} added resources it must not add: {len(new)}")
        # A filtered apply never reaches outside its scope.
        if sub is not None:
            v += [f"{op} scoped to one subscription changed {k} outside it"
                  for k in sorted(gone | new) if segments(k)[1] != arm_id_key(sub)]
        if types is not None:
            v += [f"{op} scoped to {types} removed {k} of another type" for k in sorted(gone)
                  if pre_types.get(k) not in types]
        else:
            # inv 8 for drift: disappear only removes leaves, so nothing it removed may
            # still have a live descendant (by canonical identity, as the DELETE cascade
            # sees it). A type-scoped apply sees only its own type's rows, so it is
            # excluded here.
            post_segs = [segments(i) for i in after["ids"]]
            v += [f"{op} removed {k} while a descendant stays live" for k in sorted(gone)
                  if any(is_descendant(segments(k), c) for c in post_segs)]
        self.shadow.refresh(after["ids"], after["overlay"])
        self.shadow.note_ledger(OP_REGISTRY[op].ledger_effect, latest["result"], scope)
        self.active_batches.append(bid)
        self._check(v, op)

    @rule(drift_type=st.sampled_from(["chaos", "temporal"]), seed=st.integers(0, 10_000),
          intensity=st.sampled_from(["0.1", "0.3", "2"]),
          scope_kind=st.sampled_from(["tenant", "subscription", "type"]), scope_pick=_PICK)
    def drift_apply(self, drift_type, seed, intensity, scope_kind, scope_pick):
        # A temporal apply also runs the appear/disappear lifecycle; chaos is fields-only.
        # Filtered scopes (--subscription / --resource-types) fingerprint only their rows.
        expect = "fields" if drift_type == "chaos" else "any"
        scope: tuple = (None, None)
        if scope_kind == "subscription":
            scope = (self.h.subscriptions[scope_pick % len(self.h.subscriptions)], None)
        elif scope_kind == "type":
            scope = (None, (self.h.types[scope_pick % len(self.h.types)],))
        STATS["scoped_applies"][scope_kind] = STATS["scoped_applies"].get(scope_kind, 0) + 1
        self._apply("drift_apply", ["--type", drift_type, "--seed", str(seed),
                                    "--intensity", intensity], expect, scope)

    # Absolute counts only: a fractional 1.0 would mint one leaf per eligible leaf on
    # every appear and grow the tenant geometrically across a long example.
    @rule(seed=st.integers(0, 10_000), intensity=st.sampled_from(["2", "3"]))
    def drift_appear(self, seed, intensity):
        self._apply("drift_appear", ["--type", "temporal", "--codes", "DRIFT_APPEAR",
                                     "--seed", str(seed), "--intensity", intensity], "appear")

    @rule(seed=st.integers(0, 10_000), intensity=st.sampled_from(["1", "2"]))
    def drift_disappear(self, seed, intensity):
        self._apply("drift_disappear", ["--type", "temporal", "--codes", "DRIFT_DISAPPEAR",
                                        "--seed", str(seed), "--intensity", intensity],
                    "disappear")

    @precondition(lambda self: self.active_batches)
    @rule(pick=_PICK)
    def drift_revert(self, pick):
        bid = self.active_batches[pick % len(self.active_batches)]
        before = self.h.snap()
        code, out = run_cli_inprocess("revert-drift", "--batch-id", bid,
                                      "--database-url", self.h.url)
        self._record("drift_revert", f"batch={bid[:8]} -> exit {code}", code == 0)
        self._check([] if code == 0 else [f"revert-drift failed (exit {code}): {out[-600:]}"],
                    "drift_revert")
        after = self.h.snap()
        v = user_row_violations(before["overlay"], after["overlay"])  # inv 3
        v += revision_violations(before["overlay"], after["overlay"], before["seq"], self.rev0)
        if not next(b for b in self.h.batches() if b["batch_id"] == bid)["reverted"]:
            v.append("reverted batch is not marked reverted")
        pre = {arm_id_key(r) for r in before["ids"]}
        post = {arm_id_key(r) for r in after["ids"]}
        v += [f"revert resurrected user-deleted {k}" for k in sorted(post - pre)
              if self.shadow.entries.get(k, {}).get("source") == "user"]
        v += [f"revert removed user-owned {k}" for k in sorted(pre - post)
              if self.shadow.entries.get(k, {}).get("source") == "user"]
        self.active_batches.remove(bid)
        self.shadow.refresh(after["ids"], after["overlay"])
        self.shadow.note_ledger(OP_REGISTRY["drift_revert"].ledger_effect)
        self._check(v, "drift_revert")

    # ---- global invariants, after EVERY step ------------------------------------------ #

    @invariant()
    def global_invariants(self):
        rows = self.h.resolved_rows()
        ids = [r["id"] for r in rows]
        v = duplicate_keys(ids)  # inv 1 (Python fold)
        v += [f"identity key {k} resolves to {n} rows (database fold)"
              for k, n in self.h.db_duplicate_keys()]
        v += served_mismatches(self.h.http_list(), rows)  # inv 2
        v += shadow_mismatches(self.shadow, ids)  # inv 2 (shadow leg)
        seq = self.h.seq()
        if seq < ArmConformance.last_seq_seen:
            v.append("revision sequence went backwards")  # inv 7
        ArmConformance.last_seq_seen = max(ArmConformance.last_seq_seen, seq)
        self._check(v, "global invariants")


# --------------------------------------------------------------------------------------- #
# The live run
# --------------------------------------------------------------------------------------- #


@pytest.mark.conformance
def test_arm_state_model_conformance(conformance_db, conformance_server, conformance_pg):
    """Given the real server + drift CLI on a dedicated PG16 database, When Hypothesis
    drives generated op sequences, Then the global invariants hold after every op, and
    every registered op actually executed (non-vacuity)."""
    harness = build_harness(conformance_db, conformance_server, conformance_pg)
    STATS.update({"examples": 0, "steps": 0, "ops": {}, "cascades": 0, "chain_checks": 0,
                  "scoped_applies": {}, "resurrections": 0, "resurrect_casing_checks": 0})
    run_state_machine_as_test(lambda: ArmConformance(harness), settings=CONFORMANCE_SETTINGS)
    report = (
        f"conformance run: {STATS['examples']} examples, {STATS['steps']} steps "
        f"(max_examples={_EXAMPLES}, stateful_step_count={_STEPS}); successful ops: "
        + ", ".join(f"{k}={STATS['ops'].get(k, 0)}" for k in OP_REGISTRY)
        + f"; deletes that cascaded={STATS['cascades']}, "
        f"ledger-chain checks={STATS['chain_checks']}, "
        f"resurrections={STATS['resurrections']} "
        f"(case-variant={STATS['resurrect_casing_checks']}), "
        f"drift_apply scopes={dict(sorted(STATS['scoped_applies'].items()))}"
    )
    print(report)
    missing = [op for op in OP_REGISTRY if STATS["ops"].get(op, 0) == 0]
    assert not missing, f"vacuous run: registered ops never executed {missing}\n{report}"
    assert STATS["cascades"] > 0, f"vacuous run: no delete ever cascaded\n{report}"
    assert STATS["chain_checks"] > 0, f"vacuous run: ledger chaining never checked\n{report}"
    assert STATS["resurrect_casing_checks"] > 0, (
        f"vacuous run: no case-variant tombstone resurrection was checked\n{report}"
    )
    filtered = STATS["scoped_applies"].get("subscription", 0) + STATS["scoped_applies"].get(
        "type", 0
    )
    assert filtered > 0, f"vacuous run: no filtered-scope drift apply ran\n{report}"


# --------------------------------------------------------------------------------------- #
# Pure model / predicate tests (default suite, no live infrastructure)
# --------------------------------------------------------------------------------------- #

_SUB = "00000000-0000-0000-0000-000000000001"
_S1 = route_id(_SUB, "rg", "Microsoft.Sql/servers/s1")
_S10 = route_id(_SUB, "rg", "Microsoft.Sql/servers/s10")
_S1_DB = route_id(_SUB, "rg", "Microsoft.Sql/servers/s1/databases/d1")


def test_every_registered_op_has_exactly_one_machine_rule():
    # Given the op registry, Then the machine's rules are exactly the registered ops, so
    # a new op cannot be registered without a rule (or ruled without being registered).
    rules = {
        name
        for name, fn in vars(ArmConformance).items()
        if hasattr(fn, "hypothesis_stateful_rule")
    }
    assert rules == set(OP_REGISTRY)


def test_registry_is_the_current_pre_resource_group_surface_only():
    assert {s.category for s in OP_REGISTRY.values()} == {"arm", "drift"}
    assert not [n for n in OP_REGISTRY if "group" in n or n.startswith("rg")]
    assert {s.ledger_effect for s in OP_REGISTRY.values()} == {
        LEDGER_MINT, LEDGER_BREAK, LEDGER_NONE
    }


def test_segment_containment_never_matches_a_sibling_lookalike():
    # Given servers/s1 and servers/s10, Then only the true child is a descendant.
    assert is_descendant(segments(_S1), segments(_S1_DB))
    assert not is_descendant(segments(_S1), segments(_S10))
    assert not is_descendant(segments(_S1), segments(_S1))
    # Case-insensitive on the canonical fold.
    assert is_descendant(segments(_S1.upper().replace("RESOURCEGROUPS", "resourceGroups")),
                         segments(_S1_DB))


def test_shadow_delete_cascades_present_descendants_only():
    # Given a parent with a child and a sibling lookalike
    shadow = Shadow.from_db([_S1, _S10, _S1_DB], {})
    # When the parent is deleted
    cascaded = shadow.delete(_S1)
    # Then the parent + child are user tombstones and the sibling is untouched
    assert cascaded == [arm_id_key(_S1_DB)]
    assert not shadow.is_present(_S1) and not shadow.is_present(_S1_DB)
    assert shadow.is_present(_S10)
    assert shadow.entries[arm_id_key(_S1)]["source"] == "user"


def test_shadow_delete_of_a_never_seen_id_still_records_a_user_tombstone():
    shadow = Shadow()
    shadow.delete(_S1)
    assert shadow.entries[arm_id_key(_S1)] == {"id": _S1, "present": False, "source": "user"}


def test_shadow_carries_no_revision_or_etag_state():
    shadow = Shadow.from_db([_S1], {})
    shadow.put(_S1_DB)
    for entry in shadow.entries.values():
        assert set(entry) == {"id", "present", "source"}


def test_ledger_continuity_resets_on_arm_write_or_revert():
    shadow = Shadow()
    # Given a successful apply, Then the chain is continuous from its result
    shadow.note_ledger(LEDGER_MINT, "fp1")
    assert shadow.ledger_continuous and shadow.last_result_fp == "fp1"
    # When a stale (non-mutating) write happens, Then the chain still holds
    shadow.note_ledger(LEDGER_NONE)
    assert shadow.ledger_continuous and shadow.last_result_fp == "fp1"
    # When an ARM write or a revert happens, Then the chain is broken
    shadow.note_ledger(LEDGER_BREAK)
    assert not shadow.ledger_continuous and shadow.last_result_fp is None


def test_duplicate_keys_flags_case_variants_of_one_identity():
    assert duplicate_keys([_S1, _S10]) == []
    assert duplicate_keys([_S1, _S1.replace("servers/s1", "servers/S1")]) != []


def _row(rid, **over):
    row = {"id": rid, "name": rid.rsplit("/", 1)[-1], "type": "Microsoft.Sql/servers",
           "location": "eastus", "tags": {}, "sku": None, "kind": None, "properties": {}}
    row.update(over)
    return row


def test_served_mismatches_detects_missing_extra_and_differing_bodies():
    rows = [_row(_S1), _row(_S10)]
    http = {arm_id_key(_S1): served_body(rows[0]),
            arm_id_key(_S1_DB): served_body(_row(_S1_DB))}
    http[arm_id_key(_S1)] = {**http[arm_id_key(_S1)], "tags": {"x": "1"}}
    v = served_mismatches(http, rows)
    assert any("missing from the HTTP list" in s for s in v)
    assert any("does not hold" in s for s in v)
    assert any("['tags']" in s for s in v)
    # Type casing alone is not a mismatch (the server serves canonical casing).
    ok = {arm_id_key(_S1): {**served_body(rows[0]), "type": "MICROSOFT.SQL/SERVERS"},
          arm_id_key(_S10): served_body(rows[1])}
    assert served_mismatches(ok, rows) == []


def test_served_body_omits_null_sku_and_kind():
    assert "sku" not in served_body(_row(_S1)) and "kind" not in served_body(_row(_S1))
    assert served_body(_row(_S1, properties=None))["properties"] == {}


def _ov(source, rev, present=True, body="{}"):
    return {"id": _S1, "present": present, "source": source, "revision": rev, "body": body}


def test_user_row_violations_flags_modified_or_removed_user_rows_only():
    before = {"a": _ov("user", 5), "b": _ov("drift", 6)}
    assert user_row_violations(before, {"a": _ov("user", 5)}) == []  # drift row may go
    assert user_row_violations(before, {"a": _ov("user", 9)})
    assert user_row_violations(before, {"b": _ov("drift", 6)})


def test_revision_violations_are_relative_to_the_pre_op_sequence():
    before = {"a": _ov("user", 5)}
    # untouched row keeps its revision: fine
    assert revision_violations(before, {"a": _ov("user", 5)}, seq_before=7, rev0=1) == []
    # rewritten row above the pre-op sequence: fine
    assert revision_violations(before, {"a": _ov("user", 8, body="{1}")}, 7, 1) == []
    # rewritten row NOT above the pre-op sequence: violation
    assert revision_violations(before, {"a": _ov("user", 6, body="{1}")}, 7, 1)
    # content changed under the same revision: violation
    assert revision_violations(before, {"a": _ov("user", 5, body="{1}")}, 7, 1)
    # a row older than the example start: violation
    assert revision_violations({}, {"a": _ov("user", 3)}, seq_before=2, rev0=4)


def test_cascade_violations_flags_a_surviving_descendant():
    assert cascade_violations(_S1, [_S1, _S1_DB, _S10], [_S10]) == []
    assert cascade_violations(_S1, [_S1, _S1_DB, _S10], [_S1_DB, _S10])


def test_a_forced_invariant_break_reports_the_op_trace():
    # Given a trace of ops and a broken invariant, When it is reported, Then the report
    # names the invariant, each violation, and the numbered op trace (ids/status only).
    report = violation_report(
        "global invariants", ["identity key k resolves to 2 rows"],
        ["arm:put_new id=/subscriptions/x -> 201", "drift:drift_apply --type chaos -> exit 0"],
    )
    assert "global invariants" in report and "resolves to 2 rows" in report
    assert "  1. arm:put_new" in report and "  2. drift:drift_apply" in report
    assert "--hypothesis-seed" in report
    assert format_trace([]) == "  (no ops)"


def test_ledger_chain_is_only_expected_within_the_same_drift_scope():
    # Given a successful apply over one subscription scope
    shadow = Shadow()
    shadow.note_ledger(LEDGER_MINT, "fp-sub-a", scope=("sub-a", None))
    # Then the next apply over the SAME scope must chain to it
    assert shadow.chain_expected(("sub-a", None)) == "fp-sub-a"
    # But an apply over a DIFFERENT scope fingerprints a different row set: no chain
    assert shadow.chain_expected((None, ("Microsoft.Storage/storageAccounts",))) is None
    assert shadow.chain_expected((None, None)) is None
    # And after an ARM write / revert nothing chains
    shadow.note_ledger(LEDGER_BREAK)
    assert shadow.chain_expected(("sub-a", None)) is None
