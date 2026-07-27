import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, type ReactNode } from 'react';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { armGet, simGet, ArmError } from './client';
import {
  useViolations,
  useSummary,
  useResourceGroups,
  violationsUrl,
  dependenciesUrl,
  resourcesUrl,
} from './queries';

/**
 * The MOCK-09 Bearer boundary (make-or-break integration fact): ARM `/subscriptions/**` calls MUST
 * carry a placeholder `Authorization: Bearer` header; the bearer-EXEMPT `/_sim/**` calls MUST NOT.
 * Getting this wrong yields "KPIs render, tree 401s". These tests pin BOTH sides plus ARM error
 * parsing (MOCK-10) and the query-hook URL contract every Explorer view consumes.
 */

const fetchMock = vi.fn();

function okJson(body: unknown) {
  return { ok: true, status: 200, statusText: 'OK', json: async () => body } as Response;
}

function errJson(status: number, body: unknown) {
  return {
    ok: false,
    status,
    statusText: 'Error',
    json: async () => body,
  } as unknown as Response;
}

function errNonJson(status: number, statusText: string) {
  return {
    ok: false,
    status,
    statusText,
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON');
    },
  } as unknown as Response;
}

function hookWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

const ARM_ID =
  '/subscriptions/b7e2/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/acct';

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('armGet — ARM /subscriptions calls carry the placeholder Bearer (MOCK-09)', () => {
  it('attaches Authorization: Bearer tenantless-ui to an ARM request', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ id: ARM_ID }));
    await armGet('/subscriptions/b7e2/resourceGroups');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0];
    const headers = (init?.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer tenantless-ui');
  });

  it('returns the parsed JSON body on 2xx', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ id: ARM_ID, name: 'acct' }));
    const body = await armGet<{ name: string }>('/subscriptions/b7e2/resourceGroups');
    expect(body.name).toBe('acct');
  });
});

describe('armGet — same-origin guard (WR-01): fail closed on non-relative paths', () => {
  it('rejects an absolute cross-origin URL WITHOUT issuing an authenticated fetch', async () => {
    await expect(armGet('https://attacker.example/collect')).rejects.toBeInstanceOf(ArmError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects a protocol-relative //evil/x URL WITHOUT fetching', async () => {
    await expect(armGet('//evil.example/x')).rejects.toBeInstanceOf(ArmError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('still attaches the Bearer and fetches for a legitimate relative ARM path', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ id: ARM_ID }));
    await armGet(ARM_ID);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(ARM_ID);
    const headers = (init?.headers ?? {}) as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer tenantless-ui');
  });
});

describe('simGet — same-origin guard (WR-01): fail closed on non-relative paths', () => {
  it('rejects an absolute cross-origin URL WITHOUT fetching', async () => {
    await expect(simGet('https://attacker.example/_sim/summary')).rejects.toBeInstanceOf(ArmError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('rejects a protocol-relative //evil/_sim URL WITHOUT fetching', async () => {
    await expect(simGet('//evil.example/_sim/summary')).rejects.toBeInstanceOf(ArmError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('still fetches a legitimate relative /_sim path', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ totals: {} }));
    await simGet('/_sim/summary');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/_sim/summary');
  });
});

describe('simGet — /_sim calls are bearer-EXEMPT (no Authorization header)', () => {
  it('sends NO Authorization header for a /_sim request', async () => {
    fetchMock.mockResolvedValueOnce(okJson({ totals: {} }));
    await simGet('/_sim/summary');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0];
    const headers = (init?.headers ?? {}) as Record<string, string> | undefined;
    expect(headers?.Authorization).toBeUndefined();
  });
});

describe('toArmError — non-2xx bodies parse to { code, message } (MOCK-10)', () => {
  it('throws an ArmError exposing code + message on a 400 InvalidFilter', async () => {
    fetchMock.mockResolvedValueOnce(
      errJson(400, { error: { code: 'InvalidFilter', message: 'bad filter' } }),
    );
    await expect(armGet('/subscriptions/b7e2/resourceGroups?$filter=nope')).rejects.toMatchObject({
      code: 'InvalidFilter',
      message: 'bad filter',
    });
  });

  it('throws with the parsed CloudError code/message on a 404 (not a raw Response)', async () => {
    fetchMock.mockResolvedValueOnce(
      errJson(404, { error: { code: 'ResourceNotFound', message: 'no such resource' } }),
    );
    const err = (await armGet(ARM_ID).catch((e) => e)) as ArmError;
    expect(err).toBeInstanceOf(ArmError);
    expect(err.code).toBe('ResourceNotFound');
    expect(err.message).toBe('no such resource');
  });

  it('falls back to a status-based message when the error body is not JSON', async () => {
    fetchMock.mockResolvedValueOnce(errNonJson(502, 'Bad Gateway'));
    const err = (await simGet('/_sim/summary').catch((e) => e)) as ArmError;
    expect(err).toBeInstanceOf(ArmError);
    expect(err.code).toBe('502');
    expect(String(err.message).length).toBeGreaterThan(0);
  });
});

describe('URL builders — the composed query strings each Explorer view consumes', () => {
  it('violationsUrl builds ?resource=<encoded armId>', () => {
    expect(violationsUrl({ resource: ARM_ID })).toBe(
      `/_sim/violations?resource=${encodeURIComponent(ARM_ID)}`,
    );
  });

  it('violationsUrl builds ?subscription=<uuid>', () => {
    expect(violationsUrl({ subscription: 'b7e2-1c4a' })).toBe(
      '/_sim/violations?subscription=b7e2-1c4a',
    );
  });

  it('violationsUrl appends severity + code filters when given', () => {
    const url = violationsUrl({ resource: ARM_ID, severity: 'High', code: 'STORAGE_HTTPS' });
    expect(url).toContain('severity=High');
    expect(url).toContain('code=STORAGE_HTTPS');
  });

  it('dependenciesUrl builds subscription + type + $skiptoken params', () => {
    const url = dependenciesUrl({ subscription: 'b7e2', type: 'private-endpoint', skipToken: 'tok' });
    expect(url).toContain('subscription=b7e2');
    expect(url).toContain('type=private-endpoint');
    expect(url).toContain('%24skiptoken=tok');
  });

  it('resourcesUrl composes an ARM list URL with $top and an optional $filter', () => {
    const url = resourcesUrl('b7e2', 'rg', { filter: "location eq 'westeurope'" });
    expect(url.startsWith('/subscriptions/b7e2/resourceGroups/rg/resources?')).toBe(true);
    expect(url).toContain('%24top=100');
    expect(url).toContain('%24filter=');
  });

  it('resourcesUrl encodeURIComponent-encodes sub/rg so a value cannot inject a query/path (WR-02)', () => {
    const url = resourcesUrl('a/b?x=1', 'r g');
    // The raw injection chars must NOT appear as an un-encoded path segment.
    expect(url).not.toContain('/a/b?x=1/');
    expect(url).toContain(`/subscriptions/${encodeURIComponent('a/b?x=1')}/`);
    expect(url).toContain(`/resourceGroups/${encodeURIComponent('r g')}/resources?`);
    // The only `?` in the URL is the query-string separator introduced by the builder.
    expect(url.split('?').length).toBe(2);
  });
});

describe('query hooks — wiring the builders through simGet/armGet', () => {
  it('useViolations({resource}) fires simGet against the resource-scoped URL (no auth header)', async () => {
    fetchMock.mockResolvedValue(okJson({ count: 0, value: [] }));
    renderHook(() => useViolations({ resource: ARM_ID }), { wrapper: hookWrapper() });

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith(violationsUrl({ resource: ARM_ID }));
    const [, init] = fetchMock.mock.calls[0];
    expect((init as RequestInit | undefined)?.headers).toBeUndefined();
  });

  it('useSummary() fires simGet against /_sim/summary', async () => {
    fetchMock.mockResolvedValue(okJson({ totals: {} }));
    renderHook(() => useSummary(), { wrapper: hookWrapper() });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/_sim/summary'));
  });

  it('useResourceGroups encodeURIComponent-encodes the subscription segment (WR-02)', async () => {
    fetchMock.mockResolvedValue(okJson({ value: [] }));
    renderHook(() => useResourceGroups('a/b?x=1'), { wrapper: hookWrapper() });

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    // armGet passes (url, { headers }); assert on the encoded URL arg only.
    expect(fetchMock.mock.calls[0][0]).toBe(
      `/subscriptions/${encodeURIComponent('a/b?x=1')}/resourceGroups`,
    );
  });

  it('useViolations with no params stays disabled (enabled guard — never fetches)', async () => {
    renderHook(() => useViolations({}), { wrapper: hookWrapper() });
    // give the query a tick; it must not fire without a resource/subscription scope
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
