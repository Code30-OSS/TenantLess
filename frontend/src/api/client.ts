/**
 * The Bearer-aware fetch client — the make-or-break integration seam (MOCK-09, RESEARCH Pattern 5).
 *
 * `armGet` attaches a PLACEHOLDER `Authorization: Bearer <token>` header to the ARM data routes
 * (`/subscriptions/**`), which sit behind the any-Bearer gate — WITHOUT it every tree/detail/filter
 * call 401s while the `/_sim` header KPIs still render (RESEARCH Pitfall 1, the confusing partial
 * failure). `simGet` sends NO Authorization header for the bearer-EXEMPT `/_sim/**` overlay routes.
 *
 * The placeholder Bearer is the documented **local-dev** model (IAM-05, `--enforce-auth` OFF by
 * default): any non-empty token → 200. Real JWT validation under `--enforce-auth` ON is **out of
 * scope** this phase — the UI would then need `/token` (a JWKS-verified token), which is NOT built
 * here. Shipping a real token bypass is explicitly avoided (threat T-15-14, accepted/documented).
 *
 * Both wrappers parse a non-2xx body into the ARM `{ error: { code, message } }` shape (MOCK-10) and
 * throw a typed {@link ArmError}, falling back to a status-based message when the body is not JSON.
 */

/** Any non-empty Bearer satisfies the ARM gate with `--enforce-auth` OFF (the default, IAM-05). */
const UI_BEARER = 'tenantless-ui';

/** A typed error carrying the ARM CloudError `code` alongside the human-readable `message`. */
export class ArmError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = 'ArmError';
    this.code = code;
    this.status = status;
  }
}

/** Parse a non-2xx Response into an {@link ArmError}; tolerate a non-JSON body (fallback message). */
async function toArmError(response: Response): Promise<ArmError> {
  try {
    const body = (await response.json()) as { error?: { code?: string; message?: string } };
    const code = body?.error?.code;
    const message = body?.error?.message;
    if (code && message) {
      return new ArmError(code, message, response.status);
    }
  } catch {
    // Body was not JSON (e.g. a proxy HTML error page) — fall through to the status-based error.
  }
  const statusText = response.statusText || 'Request failed';
  return new ArmError(String(response.status), `${response.status} ${statusText}`, response.status);
}

/**
 * Fail closed on any path that is not a same-origin, root-relative path (WR-01, threat T-15-14).
 *
 * The `?res=` deep-link param flows unchecked into `armGet` (ResourcesView → useResourceDetail); a
 * crafted absolute cross-origin URL would otherwise cause the browser to issue an AUTHENTICATED
 * cross-origin GET carrying the Bearer header to an attacker origin. We reject anything that isn't a
 * single-slash root-relative path: `//host` (protocol-relative) and `scheme://host` (absolute) are
 * refused, and we belt-and-braces resolve against the current origin to assert it never escapes.
 */
function assertSameOrigin(path: string): void {
  // Root-relative only: reject protocol-relative `//…` and any absolute `scheme://…` URL.
  if (!path.startsWith('/') || path.startsWith('//')) {
    throw new ArmError('BadRequest', `Refusing to fetch non-relative path: ${path}`, 0);
  }
  const resolved = new URL(path, window.location.origin);
  if (resolved.origin !== window.location.origin) {
    throw new ArmError('BadRequest', `Refusing to fetch cross-origin path: ${path}`, 0);
  }
}

/** GET an ARM `/subscriptions/**` route WITH the placeholder Bearer (MOCK-09). Throws on non-2xx. */
export async function armGet<T>(path: string): Promise<T> {
  assertSameOrigin(path);
  const response = await fetch(path, {
    headers: { Authorization: `Bearer ${UI_BEARER}` },
  });
  if (!response.ok) throw await toArmError(response);
  return (await response.json()) as T;
}

/** GET a bearer-EXEMPT `/_sim/**` route with NO Authorization header. Throws on non-2xx. */
export async function simGet<T>(path: string): Promise<T> {
  assertSameOrigin(path);
  const response = await fetch(path);
  if (!response.ok) throw await toArmError(response);
  return (await response.json()) as T;
}

// ---------------------------------------------------------------------------
// Control plane (Phase 17, CTRL-01/CTRL-05) — the app's FIRST write surface.
// ---------------------------------------------------------------------------
//
// The `/_control/*` routes sit behind a DISTINCT auth realm from the ARM Bearer: a
// server-minted secret presented in an `X-Control-Token` header (D-01/D-17). That token
// spawns subprocesses (`generate`/`analyze`/`reset`), so it is the most sensitive value the
// browser holds. It lives in a SINGLE module-scoped variable and is NEVER persisted — no
// `localStorage`, no cookie, no TanStack `queryKey`, no URL, no log (threat T-17-05). On a
// 401/403 the consuming view calls `setControlToken(null)` to drop back to the locked gate.
//
// The custom header (not a cookie) is also the CSRF mitigation (T-17-09): it is never
// ambiently attached by the browser, and every control fetch reuses {@link assertSameOrigin}
// (WR-01) + {@link toArmError} — exactly like {@link armGet}/{@link simGet}, only the header differs.

/** The in-memory control token. Module-scoped ONLY — reset on reload, never written to storage. */
let controlToken: string | null = null;

/**
 * Set (or clear, with `null`) the in-memory control token. This is the ONLY writer of the secret,
 * and it writes to memory alone — never `localStorage`, a cookie, or any persisted store. Views call
 * `setControlToken(null)` on a 401/403 to re-lock the control plane.
 */
export function setControlToken(token: string | null): void {
  controlToken = token;
}

/** Read the in-memory control token (`null` when the control plane is locked). */
export function getControlToken(): string | null {
  return controlToken;
}

/** Build control headers, attaching `X-Control-Token` ONLY when a token is set (never a null header). */
function controlHeaders(withJsonBody: boolean): Record<string, string> {
  const headers: Record<string, string> = {};
  if (withJsonBody) headers['Content-Type'] = 'application/json';
  if (controlToken !== null) headers['X-Control-Token'] = controlToken;
  return headers;
}

/** POST a JSON body to a same-origin `/_control/**` route with the in-memory token. Throws on non-2xx. */
export async function controlPost<T>(path: string, body: unknown): Promise<T> {
  assertSameOrigin(path);
  const response = await fetch(path, {
    method: 'POST',
    headers: controlHeaders(true),
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await toArmError(response);
  return (await response.json()) as T;
}

/** GET a same-origin `/_control/**` route with the in-memory token. Throws on non-2xx. */
export async function controlGet<T>(path: string): Promise<T> {
  assertSameOrigin(path);
  const response = await fetch(path, { headers: controlHeaders(false) });
  if (!response.ok) throw await toArmError(response);
  return (await response.json()) as T;
}

/** DELETE a same-origin `/_control/**` route with the in-memory token (snapshot delete). Throws on non-2xx. */
export async function controlDelete<T>(path: string): Promise<T> {
  assertSameOrigin(path);
  const response = await fetch(path, { method: 'DELETE', headers: controlHeaders(false) });
  if (!response.ok) throw await toArmError(response);
  // The snapshot-delete endpoint returns `204 No-Content` with an EMPTY body — calling
  // `response.json()` on it throws ("Unexpected end of JSON input"), which used to make a
  // successful delete spuriously reject. On a no-content response resolve without parsing.
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
