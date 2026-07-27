# Profiles

A **profile** is the statistical specification an estate is generated from. It is a plain
JSON file validated against [`profiles/schema.json`](../profiles/schema.json), so you can
write one by hand, commit it, diff it and review it in a pull request.

## What is in one

| Section | Describes |
|---------|-----------|
| `source_stats` | Overall scale: subscriptions, resource groups, resources |
| `subscription_archetypes` | Kinds of subscription, their relative weight, size distributions and region mix |
| `resource_group_templates` | Recurring resource-group compositions (which types appear together) and their sizes |
| `resource_type_distributions` | Per-type frequency, plus property / SKU / kind value histograms |
| `tag_distributions` | Tag key frequencies, value histograms, key co-occurrence, per-type untagged rates |
| `cross_subscription_dependencies` | Hub-spoke, shared Key Vault, centralized logging, shared registry, private-endpoint rates |
| `governance_violations` | Per-violation-type injection rates |
| `cost_distributions` | Fitted monthly-cost distribution per resource type |
| `naming_conventions` | Tokenized structural name patterns — never a verbatim name |
| `provenance` | Where this profile came from, and whether anything real is upstream of it |

Everything is aggregate. A profile never contains an identifier: no subscription ID, no
resource name, no raw tag value from a real estate.

## Bundled profiles

| Profile | Scale | Origin |
|---------|-------|--------|
| `enterprise` | ~250 subscriptions, ~4.3K resource groups, ~59K resources, 38 resource types | Generated, then analyzed — see below |
| `small` | 50 subscriptions, 600 resource groups, 5K resources | Hand-authored |

Use them by name: `--profile enterprise`. Any path is also accepted, and an existing file
path wins over a bundled name.

## Provenance

Every bundled profile declares, in machine-readable form, whether anything real is upstream
of it:

```json
"provenance": {
  "synthetic": true,
  "derived_from_real_tenant": false,
  "derivation": {
    "bootstrap_profile": "profiles/oss-bootstrap.json",
    "bootstrap_profile_sha256": "…",
    "generator_seed": 20260726,
    "estate": { "subscriptions": 250, "resource_groups": 4301, "resources": 59280 },
    "steps": ["…"]
  }
}
```

This matters more than it might look. A profile fitted from a real Azure tenant encodes that
organization's resource-type mix, tag vocabulary, subscription count and cost shape — even
after every identifier is stripped. Stripping identifiers is not the same as having the
right to publish the statistics. So the bundled profiles are not anonymized real data; they
have no real ancestor at all.

`scripts/check_release_provenance.py` enforces this, and it checks measurements too: a
benchmark computed from a real-derived profile carries that estate's shape onward even if
the profile itself is withheld.

```bash
uv run python scripts/check_release_provenance.py --tree .
```

## Rebuilding the `enterprise` profile

The bundled `enterprise` profile is not hand-written and not fitted from real data. It is
produced by a five-step chain that anyone can re-run:

```bash
# 1. Build the hand-authored bootstrap profile.
#    Every number in it is authored from public Azure concepts — CAF landing-zone
#    archetypes, public ARM type names, public region names, conventional tagging.
uv run python scripts/build_oss_bootstrap_profile.py

# 2. Generate a synthetic estate from it.
uv run tenantless generate --profile profiles/oss-bootstrap.json \
    --seed 20260726 --cost-as-of 2026-07-01 --jobs 0 --force

# 3. Export that estate as a scan the analyzer can read.
uv run python scripts/export_estate_duckdb.py --out build/estate.duckdb --force

# 4. Analyze the generated estate back into a profile.
uv run tenantless analyze --source duckdb:build/estate.duckdb \
    --out build/enterprise.json --allow-no-denylist --non-interactive --k 5

# 5. Record the provenance.
uv run python scripts/stamp_synthetic_provenance.py \
    --profile build/enterprise.json \
    --bootstrap profiles/oss-bootstrap.json \
    --seed 20260726 --cost-as-of 2026-07-01
```

Step 4 uses `--allow-no-denylist` because the source is a synthetic estate with no real
identifiers to guard against. **Never use that flag on a real scan** — the denylist is the
data boundary's fail-closed guard.

The round trip is lossy by design: the analyzer's minimum-aggregation threshold drops the
rarest resource types, so the re-derived profile has a slightly shorter tail than the
bootstrap. That is the same folding a real scan gets, which is the point — the published
profile exercises the same code path a user's own profile will.

## Fitting a profile from your own scan

```bash
uv run tenantless analyze \
    --source duckdb:/path/to/your-scan.duckdb \
    --out profiles/mine.json \
    --denylist profiles/.my-denylist.json \
    --min-bucket-size 5
```

Then generate from it:

```bash
uv run tenantless generate --profile profiles/mine.json --seed 42
```

### The denylist is not optional for real data

`--denylist` points at a JSON file of real identifiers — subscription names, resource names,
raw tag values — that must never appear in the output. The analyzer scans its own output
against it and fails closed.

Analyzing a real scan without one requires `--allow-no-denylist`, which exists for sample
and synthetic sources only. Build a denylist with `scripts/build-denylist.py`.

Denylists and real-derived profiles are gitignored by pattern (`profiles/.*-denylist.json`,
`profiles/*-real.json`). Keep them that way.

### Profiling a live tenant

```bash
uv sync --extra azure
uv run tenantless analyze --source "azure:sub-id-1,sub-id-2" --out profiles/mine.json \
    --denylist profiles/.my-denylist.json
```

This queries Azure Resource Graph read-only through `DefaultAzureCredential` and derives its
own denylist terms from what it reads, in addition to the file you supply.

## Writing one by hand

`profiles/oss-bootstrap.json` and its builder,
[`scripts/build_oss_bootstrap_profile.py`](../scripts/build_oss_bootstrap_profile.py), are
the worked example. The builder keeps the authored tables in Python and emits the JSON, so
the weights stay readable and a self-check verifies every distribution sums to 1 before it
writes anything.

That self-check also verifies that every `ANCHOR_REQUIRED` archetype in the naming catalog
has its anchor type reachable from at least one resource-group template. Without that, the
semantic-naming path would never fire for those archetypes and the estate would silently
lose a feature you cannot see missing.
