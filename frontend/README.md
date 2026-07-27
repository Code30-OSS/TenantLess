# tenantless — web console (`frontend/`)

The React + Vite + TypeScript SPA for the Tenantless ARM mock console. Built to `dist/`, embedded
into the `tenantless-server` binary at compile time and served under the `/ui` prefix.

> **Scope: desktop-only.** This is a developer console for driving the ARM mock (scanners,
> governance tooling, cross-subscription risk modeling) from a workstation — it is not designed
> or tested for phone/tablet viewports. Target ≥ ~1024px.

## Prerequisites

- Node `^20.19 || >=22.12` (verified on Node 24.11.1), npm 11+.
- The axum server running on `http://127.0.0.1:8080` for live dev (see dev proxy below).

## Install (reproducible)

```bash
npm ci          # installs the EXACT tree from the committed package-lock.json
```

Use `npm ci` (not `npm install`) in CI / build — it is lockfile-exact and fails on drift. New deps
are added with `npm install --save-exact <pkg>@<version>` (the committed `.npmrc` sets
`save-exact=true`, so this is the default). Never commit caret/tilde ranges.

## Scripts

| Script              | Purpose                                                             |
| ------------------- | ------------------------------------------------------------------- |
| `npm run dev`       | Vite dev server at `http://localhost:5173/ui/` (proxies API → axum) |
| `npm run build`     | `tsc -b && vite build` → `dist/` (hash-named assets under `/ui/`)    |
| `npm run typecheck` | `tsc --noEmit` (app project)                                         |
| `npm run test`      | `vitest run` (single run — never watch mode)                        |
| `npm run audit`     | `npm audit --audit-level=high` (supply-chain gate)                  |

## Build-before-cargo ordering

The Rust server embeds `frontend/dist` via `include_dir` at **compile time**. Always
`npm run build` **before** `cargo build`/`cargo run`, or the binary ships the previous `dist`.
A `build.rs` freshness guard (`cargo:rerun-if-changed=../frontend/dist` + a missing-dist
hard error) backs this up, but the ordering discipline is yours.

## Dev proxy

`vite.config.ts` proxies `/_sim`, `/subscriptions`, `/token`, `/_console` to axum on
`127.0.0.1:8080`. The browser talks to a single origin (`localhost:5173`), so **no CORS is added to
axum**. `scripts/dev-proxy-smoke.sh` (repo root) documents the reachability check.

## Supply-chain posture

- Exact-pinned deps + committed `package-lock.json`; `npm ci` in build; `npm audit --audit-level=high` gate.
- Minimal footprint; no postinstall scripts on any dependency.
- Fonts (DM Sans, Space Mono) are **self-hosted** woff2 under `public/fonts/` — no CDN `<link>`/`@import`.
