# ARM Write Plane — Security & Operations

The **ARM write plane** is the simulator's generic, service-model-independent mutation
surface: `PUT` / `PATCH` / `DELETE` on the ARM resource routes
(`/subscriptions/{sub}/resourceGroups/{rg}/providers/…`), executed **synchronously** against
the overlay state store. Writes land as user-owned overlay rows; the seeded baseline is never
mutated.

The write plane is **OFF by default**. A plain `tenantless serve` is the read-only,
ARM-byte-identical scanner surface it has always been — every write method returns
`405 Method Not Allowed` with an `Allow` header until writes are explicitly armed.

> **Bare resource-group writes stay `405`.** Only resource paths that contain `/providers/…`
> are armed by the flag. A `PUT` / `PATCH` / `DELETE` on a bare
> `/subscriptions/{sub}/resourceGroups/{rg}` path returns `405` regardless of the flag —
> resource-group CRUD is a separate, later capability.

---

## 1. Arming writes (no silent enable — WAUTH-01)

Writes arm **only** via the explicit flag (or its environment variable). There is
deliberately **no** config-file or inference path — nothing else turns writes on.

```bash
# Local development: writes armed on the default loopback bind.
uv run tenantless serve --enable-arm-writes

# or via env
ENABLE_ARM_WRITES=1 uv run tenantless serve
```

| Flag | Env | Default | Purpose |
|------|-----|---------|---------|
| `--enable-arm-writes` | `ENABLE_ARM_WRITES` | `false` | Arm the `PUT`/`PATCH`/`DELETE` handlers. |
| `--allow-insecure-writes` | `ALLOW_INSECURE_WRITES` | `false` | Insecure-development override for the non-loopback refusal (§4). |
| `--enforce-auth` | `ENFORCE_AUTH` | `false` | Validate real RS256 JWTs on every ARM route (reads **and** writes). |
| `--host <addr>` | `HOST` | `127.0.0.1` | Bind interface. `0.0.0.0` / `::` are non-loopback (bind-all). |

## 2. The flag arms handlers only — it never grants auth (D-11)

`--enable-arm-writes` arms the write **handlers**. It **never**:

- grants authorization or bypasses authentication — writes pass the same `Authorization`
  gate as reads;
- bypasses the fail-closed boot guard — a structurally bad substrate still refuses to serve
  at all, armed or not.

## 3. Auth posture — writes inherit the read posture (WAUTH-02)

Writes are gated by the **same** bearer layer as reads, evaluated **before** any mutation and
**before** the `If-Match` / `If-None-Match` precondition:

- **Default (any-Bearer):** a missing or empty `Bearer` token → `401`; any non-empty token is
  accepted (the byte-identical scanner contract). This applies to writes exactly as to reads.
- **`--enforce-auth`:** the `Authorization` header must be a `Bearer <jwt>` that validates
  RS256 against this run's own JWKS (issuer / audience / expiry). An invalid or expired token
  → `401` for a write exactly as for a read.

Because the gate runs ahead of the handler body, an unauthenticated write that would also fail
a precondition returns `401` (authentication), never `412` (precondition) — authorization
structurally precedes precondition evaluation and mutation.

## 4. Unauthenticated-write startup refusal (D-12)

Enabling writes **without** `--enforce-auth` means the write plane accepts **any** request —
an unauthenticated mutation surface. That is acceptable for a knowingly-local or test
deployment, but must never be exposed on a public interface. The server therefore **refuses to
start** when unauthenticated writes are armed on a **non-loopback** bind:

| `--enable-arm-writes` | `--enforce-auth` | bind (`--host`) | `--allow-insecure-writes` | Startup |
|:---:|:---:|:---:|:---:|---|
| off | — | any | — | **Serves** (read-only). |
| on | on | any | — | **Serves** (writes authenticated). |
| on | off | loopback (`127.0.0.1`, `::1`) | — | **Serves** + WARN (§5). |
| on | off | non-loopback (`0.0.0.0`, `::`, a hostname) | off | **REFUSES** — non-zero exit, never binds. |
| on | off | non-loopback | on | **Serves** + WARN (§5). |

`0.0.0.0`, `::` (bind-all / unspecified), and any non-IP hostname are all treated as
**non-loopback** — the classifier fails safe, so only a genuine `127.0.0.0/8` or `::1` address
counts as loopback. The refusal names the host and the three remedies (add `--enforce-auth`,
bind loopback, or pass `--allow-insecure-writes`); it exits non-zero without ever binding the
port.

## 5. The startup WARN (WAUTH-03)

Whenever writes are enabled without `--enforce-auth` — on loopback, or on a non-loopback bind
under `--allow-insecure-writes` — the server emits a prominent startup **WARN** stating that
ARM writes are enabled without strict auth. This mode is **local/test-only** and MUST NOT be
exposed on a public bind. The warning is credential-safe: it names no token, `Authorization`
header, or request body.

## 6. Never logs credentials or bodies

The server never logs credentials, `Authorization` header values, or request bodies — neither
the startup WARN nor any write-path log line. Diagnostics identify a request by method and
route only.

---

*The unauthenticated-writes mode (`--enable-arm-writes` without `--enforce-auth`) is
**local/test-only**. For any shared or network-reachable deployment, run with `--enforce-auth`
so writes require a valid JWT.*
