# Contributing to Tenantless

Thanks for considering it. This document covers how to get set up, what the review bar is,
and the few areas where this project's rules are stricter than you might expect.

## Getting set up

```bash
cp .env.example .env
docker compose up -d postgres-sim     # or point DATABASE_URL at any PostgreSQL 16
uv sync
cd frontend && npm ci && npm run build && cd ..   # the Rust server embeds frontend/dist at build time
uv run tenantless generate --profile small --seed 42 --cost-as-of 2026-01-01
uv run tenantless serve
```

`tenantless serve` discovers the server binary on `PATH`, then in `target/release` /
`target/debug`, then falls back to `cargo run` — which compiles the Rust server and embeds
`frontend/dist`, so build the frontend first (above) or the compile fails.

Running the suites:

```bash
uv run pytest -q                       # Python
cargo test -p tenantless-server -- --test-threads=4   # Rust (needs Docker; bounded — see below)
cd frontend && npm ci && npm test      # Web console
```

**The full Python suite truncates the tenant in the configured database.** Several tests
generate their own estate. Regenerate before any manual UI or scanner session, and do not
point `DATABASE_URL` at anything you care about.

**The Rust suite starts one throwaway PostgreSQL container per integration test.** At
`cargo test`'s default parallelism that is one container per core, which on a many-core
machine — especially one already running other containers — overwhelms the Docker daemon and
produces a burst of `WaitContainer(StartupTimeout)` failures. These are environmental, not
assertion failures. Cap the concurrency instead:

```bash
cargo test -p tenantless-server -- --test-threads=4
```

If you see `StartupTimeout`, check the failure text before investigating the code: every one
of those is a container that never came up, not a behaviour that changed.

## How we work

- **Tests first.** A bug fix should land as a failing test and then the fix, in that order,
  as separate commits where practical. This is not ceremony — twice in this project's
  history a "fix" was only provably a fix because a repro existed first.
- **Small, atomic commits** with a conventional-commit prefix (`fix:`, `feat:`, `docs:`,
  `test:`, `chore:`).
- **Explain the why in the diff.** Comments should say why the code is shaped this way, not
  restate what it does.

## Determinism is a contract, not a nicety

A fixed `(profile, seed, cost-as-of)` must produce a **byte-identical** estate, and
`--jobs 1` must produce byte-identical output to `--jobs N`. A great deal depends on this:
reproducible bug reports, benchmark comparability, and the ability to prove a refactor
changed nothing. Cost billing periods derive from `--cost-as-of`; leave it unpinned and a
run reproduces only within the same calendar day.

If you touch the generator, prove neutrality rather than arguing it:

```bash
uv run tenantless generate --profile small --seed 42 --cost-as-of 2026-01-01 --force
# hash the generated estate before and after your change and compare
```

Anything that introduces unseeded randomness, iteration over an unordered set, or
wall-clock time into generation will break this. If you need randomness, draw it from the
seeded context in `src/tenantless/generator/rng.py`.

## The data boundary

The analyzer reads scans that may describe real tenants and writes profiles meant to be
shareable. **Every new extractor that emits strings needs its own leak test.** A new
string-emitting path silently bypasses the boundary otherwise — this has happened, and it
shipped.

Concretely, if you add an extractor:

1. Reapply the shared identifier-shaped-key guard in `src/tenantless/analyzer/privacy.py`.
2. Add a test proving your extractor cannot emit an identifier from its input.
3. Respect `--min-bucket-size`: a bucket observed too few times must be dropped, not
   published.

Never commit a denylist, a `*-real.json` profile, or anything derived from a real tenant.
`.gitignore` covers the known shapes; the scrub gate and provenance gate catch the rest.

## Adding an archetype

Resource-group names are generated from a static archetype catalog, and **a name is a claim
about what the resource group contains**. Adding or changing a catalog entry has its own
checklist: [`docs/archetype-catalog-checklist.md`](docs/archetype-catalog-checklist.md).

Read it before touching `src/tenantless/generator/archetypes.py`. The rules there keep a
generated name honest about its contents, because an automated gate that only checks
"does the rename obey its inputs" can report green while the inputs themselves over-claim.
In particular, every archetype must consciously declare a `ConfirmationPolicy` — there is no
default, so a new entry cannot inherit permissiveness by omission — and a `SUPPORTING_ALLOWED`
entry must carry a written rationale explaining why supporting signals make the claimed name
honest without an anchor. The coherence gate `scripts/audit_rg_coherence.py` enforces that a
green run reflects real coverage rather than a vacuous pass.

That rationale is metadata; it cannot make the decision correct. It exists to force a
judgment call into the diff where a reviewer can challenge it.

## Gates that must stay non-vacuous

Several gates assert that a count is zero. A gate that *cannot* fail proves nothing, so each
one carries a paired evidence floor asserting it examined a meaningful population. If you
change a gate, keep its floor, and make sure the floor counts the right population.

The scrub gate (`tests/test_scrub_gate.py`) reads its word list from a committed generic set
plus an optional gitignored private supplement (`tests/.scrub-tokens.private.json`). Forks
are expected to add their own tokens there.

## Pull requests

Run the same gates CI runs, before opening one:

```bash
# Python
uv run pytest -q
uv run python scripts/check_release_provenance.py --tree .

# Rust — cap concurrency to avoid the StartupTimeout burst described above
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test -p tenantless-server -- --test-threads=4

# Web console
cd frontend
npm ci
npm run typecheck
npm test
npm run build
```

Two pytest suites are **opt-in** — `uv run pytest -q` deselects both by default, and CI runs
them in a separate job. Run the one that matches your change:

```bash
uv run pytest -q -m integration   # live-Postgres end-to-end: serve/scan, drift, cross-sub topology
uv run pytest -q -m scale         # ~2000-sub / 500K-resource generation benchmark
```

- **`-m integration`** — run it when your change touches the end-to-end serve/scan path,
  configuration drift, cross-subscription topology, or anything that needs a live Postgres.
- **`-m scale`** — run it when your change could affect generation performance or behavior at
  large estate sizes. A scale-specific change needs the `scale` marker, not just `integration`.

In the PR description, say what you changed, why, and how you know it works. If you changed
generation, say whether output changed — and if it did not, say how you verified that.

Please open an issue first for anything large or architectural. The
[architecture doc](docs/architecture.md) is the fastest way to see where a change belongs.

## Code of conduct

Participation is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## Licensing

Tenantless is licensed under [Apache-2.0](LICENSE), and Code30 holds the copyright on the
initial published work. Contributions are submitted under that same Apache-2.0 license: you
keep the copyright in your contribution and license it to the project and its users under
Apache-2.0. There is **no Contributor License Agreement (CLA) and no copyright assignment** —
Apache-2.0 is not a copyright-assignment agreement, and this project does not currently
require anything beyond it.
