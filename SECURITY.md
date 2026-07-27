# Security Policy

## Supported versions

Security fixes land on the latest released minor version of the current major line.

| Version | Supported |
|---------|-----------|
| 1.x     | ✅ |
| < 1.0   | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report privately through GitHub's [private vulnerability
reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository: **Security → Report a vulnerability**.

Please include:

- what you observed, and what you expected instead
- the smallest reproduction you can manage — a profile, seed and command line is ideal,
  since generation is deterministic and a `(profile, seed, cost-as-of)` triple reproduces an
  estate exactly
- the versions involved (`tenantless --version`, Rust, PostgreSQL)
- the impact as you see it

You can expect an acknowledgement within 5 working days and an assessment within 15. If a
fix is warranted we will agree a disclosure timeline with you; credit is offered by default
and declined only if you ask.

## Threat model — what Tenantless is and is not

Tenantless is a **local simulator**. Understanding its posture avoids reporting design
decisions as vulnerabilities, and helps you spot the things that genuinely are.

**By design, not vulnerabilities:**

- **ARM authentication is presence-only by default.** Any non-empty `Bearer` token is
  accepted. This is a mock, not an auth gateway. `serve --enforce-auth` opts into real RS256
  JWT validation when you want to exercise a client's token handling.
- **The signing key is ephemeral and local.** Tokens minted by the built-in AAD/Entra
  endpoints are only meaningful to this server.
- **The bundled data is fake.** Subscription IDs, resource names, costs and role assignments
  are generated. They authorize nothing.
- **Generated estates contain deliberate misconfigurations.** Storage accounts allowing
  public access, NSGs open to the internet and disabled encryption are the *product* — that
  is what a governance scanner is supposed to find.

**In scope, and genuinely worth reporting:**

- **SQL injection.** Every runtime-built SQL fragment must bind literals as `$N`, never
  splice them. A path that does otherwise is a real bug, pinned by a metacharacter test.
- **Path traversal**, particularly via `--profile` and any control-plane artifact path.
- **The control-plane write surface.** It is disarmed by default and requires
  `--enable-control-plane` plus a `--control-token`. Any way to reach a write operation
  without both, to bypass the token check, or to escape `--control-data-dir`, is in scope.
- **Data-boundary escapes in the analyzer.** The analyzer must emit aggregate statistics
  only. Any path that lets a real identifier from a source scan reach a profile is a serious
  bug — see below.
- **Denial of service** through unbounded queries, memory growth or pathological `$filter`
  input.
- **Dependency vulnerabilities** with a plausible path to exploitation here.

## The data boundary

The analyzer reads scans that may describe **real** Azure tenants, and writes profiles meant
to be shareable. The boundary between those is the project's most safety-critical property.

It is enforced by, in combination:

- a **denylist scan** over real identifiers, which fails closed when no denylist is supplied
  for a real source
- **minimum-aggregation thresholds**, so a bucket observed too few times is dropped rather
  than published
- **per-extractor leak tests** — every extractor that emits strings has its own test
- a **whole-tree scrub gate** (`tests/test_scrub_gate.py`)
- a **provenance gate** (`scripts/check_release_provenance.py`), which requires every bundled
  profile to declare that it has no real-tenant ancestor

If you find a path that carries a real identifier — or a recognisable fingerprint of a real
estate — from a source scan into a profile, please report it privately. That is the highest
severity issue this project can have.

Note that a profile fitted from a real tenant still encodes that estate's *statistical
shape* even when every identifier is stripped. Anonymization is not permission: whether such
a profile may be published is a question for the data owner, not a technical property. This
is why the bundled profiles are generated rather than fitted from real data.
