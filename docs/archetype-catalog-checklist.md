# Adding an archetype — contributor checklist

The archetype catalog (`src/tenantless/generator/archetypes.py`) decides what a generated
resource group is **named**. A name is a claim about what the RG contains, so a wrong catalog
entry does not produce a cosmetic defect — it produces a tenant that lies about itself, and
every downstream consumer (scanner, governance rule, risk model) inherits the lie.

This area is deceptively easy to get wrong in a way automated gates miss: a gate can report
green while the thing it exists to check is still wrong — a name that claims more than its
contents prove. The rules below exist to close that gap, and the worked examples show the exact
shapes that slip past a naive check.

## The one rule that matters

> **A name may only claim what its contents prove.**

Everything else is machinery for keeping that true.

## Checklist

### 1. Choose the confirmation policy — there is no default

Every entry MUST declare `confirmation=`. Omitting it is a `TypeError`; there is no permissive
fallback, because a permissive fallback is exactly how an over-claiming entry slips through.

| Policy | Choose it when | Effect |
|--------|----------------|--------|
| `ANCHOR_REQUIRED` | The name asserts a **structure** — hub, platform, cluster, gateway, firewall, mesh, landing zone, registry, workload, app, db… | Only the archetype's own anchor confirms it. Supporting signals still feed the score, never the claim |
| `SUPPORTING_ALLOWED` | The name asserts a **function** that no single resource defines — backup, monitoring | An anchor confirms; so may ≥ `MIN_SUPPORTING_SIGNALS` discriminative supporting signals with a margin over the runner-up |
| `GENERIC` | The entry claims nothing (`shared` / `core`) | Never confirms; evidence tiers are not consulted |

**The test is not how the token is spelled.** There used to be a `ROLE_NOUNS` word list; it was
deleted precisely because it could only recognise roles someone had already thought of. Ask
instead: *is there a resource whose absence makes this name false?* If yes, that resource is
your anchor and the policy is `ANCHOR_REQUIRED`.

Worked example: consider `network-hub` RGs that hold route tables and NSGs but **no VNet**.
Those resources prove *networking*. They do not prove *hub*. A hub is defined by the VNet it
hubs, so `network-hub` is `ANCHOR_REQUIRED` — the VNet is the resource whose absence makes the
name false.

### 2. If you chose `SUPPORTING_ALLOWED`, write the rationale

`supporting_allowed_rationale` is **required** under this policy and **forbidden** under the
others. It must explain *why supporting signals make the claimed name honest without an
anchor*. Look at `backup` and `monitoring` for the expected depth.

Validation rejects empty text, placeholders (`TODO`, `n/a`, …), and anything under 60
characters. **It cannot tell whether your reasoning is right.** Nothing can — that is a semantic
judgement, and no test in this repo will make it for you. The field exists so the risky decision
lands in the diff where a reviewer can challenge it. Reviewers: this is the field to argue with.

Your supporting tier must also be non-empty. A `SUPPORTING_ALLOWED` entry with no supporting
signals is anchor-only in fact while advertising otherwise — declare `ANCHOR_REQUIRED` instead.

### 3. Tier the evidence honestly

- **anchor** (`required_any`) — the type that *defines* the shape.
- **supporting** (`supporting_signals`) — discriminative corroboration. Must be neither
  ubiquitous nor another archetype's anchor, and pairwise disjoint across archetypes. Enforced
  by `test_tiering_invariant_full_catalog`.
- **generic** (`generic_signals`) — co-occurs but proves nothing. Every element must be in
  `UBIQUITOUS_SIGNALS` or be another archetype's anchor.

If your archetype's only evidence is ubiquitous types, it has no evidence — an RG "certified"
on a lone storage account (a type nearly every RG carries) has proven nothing about its
workload.

### 4. Declaring a ubiquitous type as your anchor

Allowed, but it must be declared in `DECLARED_UBIQUITOUS_ANCHORS` with a written rationale.
Ubiquity means "not discriminative as **borrowed** evidence", not "meaningless" — an archetype
may own a ubiquitous type as its own anchor (`monitoring` owns `actionGroups`). The invariant
rejects both an undeclared overlap and a stale declaration.

### 5. Run the gates

```bash
uv run pytest tests/test_generator_archetypes.py tests/test_generator_pipeline.py \
              tests/test_generator_naming.py tests/test_generator_reproducibility.py \
              tests/test_scrub_gate.py -q
```

A new entry changes generated names, so also re-generate and re-run the coherence audit:

```bash
uv run tenantless generate --profile enterprise --seed 7 --cost-as-of 2026-01-01 --force
uv run python scripts/audit_rg_coherence.py
```

The audit derives everything from the catalog, so tightening the catalog tightens the gate with
no audit edit.

> **Never loosen a threshold or clear a policy to make a gate pass.** `MIN_SUPPORTING_SIGNALS`,
> `CONFIRM_MARGIN`, `MAX_SEMANTIC_CROSS_PCT` and the evidence floors are the numbers a reviewer
> trusts. Laundering one is how an over-claiming entry ships undetected. If a gate fails, report it.

### 6. Accept that the gates are a floor, not a verdict

Every automated check here can pass on a catalog entry that is still wrong. The rules above
narrow the ways an entry can be wrong; they do not establish that it is right. That judgement
is the reviewer's, and experience is the evidence that it cannot be delegated: the only check
that reliably catches the real defect is a human looking at generated names next to their
actual contents.
