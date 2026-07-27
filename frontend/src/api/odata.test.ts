import { describe, it, expect } from 'vitest';

import { buildFilter, parseSkipToken, parseTop, DEFAULT_TOP } from './odata';

describe('buildFilter', () => {
  it('composes a single resourceType clause', () => {
    expect(
      buildFilter([{ field: 'resourceType', value: 'Microsoft.Storage/storageAccounts' }]),
    ).toBe("resourceType eq 'Microsoft.Storage/storageAccounts'");
  });

  it('joins location + tagName clauses with " and " in field order', () => {
    expect(
      buildFilter([
        { field: 'location', value: 'westeurope' },
        { field: 'tagName', value: 'env' },
      ]),
    ).toBe("location eq 'westeurope' and tagName eq 'env'");
  });

  it('preserves the input clause order', () => {
    expect(
      buildFilter([
        { field: 'tagValue', value: 'prod' },
        { field: 'resourceType', value: 'Microsoft.Compute/virtualMachines' },
      ]),
    ).toBe("tagValue eq 'prod' and resourceType eq 'Microsoft.Compute/virtualMachines'");
  });

  it('returns "" for an empty clause list (upstream omits $filter entirely)', () => {
    expect(buildFilter([])).toBe('');
  });

  it("escapes a single quote in a value by OData '' doubling", () => {
    expect(buildFilter([{ field: 'tagValue', value: "O'Brien" }])).toBe(
      "tagValue eq 'O''Brien'",
    );
  });

  it('doubles every quote when several appear in one value', () => {
    expect(buildFilter([{ field: 'tagValue', value: "a'b'c" }])).toBe("tagValue eq 'a''b''c'");
  });
});

describe('parseSkipToken', () => {
  it('extracts $skipToken (spec casing) from an absolute nextLink', () => {
    expect(
      parseSkipToken(
        'http://localhost:8080/subscriptions/x/resources?$top=1000&$skipToken=abc',
      ),
    ).toBe('abc');
  });

  it('extracts $skiptoken (real server lowercase casing)', () => {
    // The axum server serializes the param as lowercase `$skiptoken`
    // (pagination.rs: `?$top={top}&$skiptoken={next_token}`) — the parser must accept it.
    expect(
      parseSkipToken(
        'http://localhost:8080/subscriptions/x/resources?$top=100&$skiptoken=Zm9v',
      ),
    ).toBe('Zm9v');
  });

  it('works on a relative nextLink (never follows the absolute origin — Pitfall 3)', () => {
    expect(parseSkipToken('/subscriptions/x/resources?$top=50&$skiptoken=tok')).toBe('tok');
  });

  it('returns null when the nextLink carries no skip token', () => {
    expect(parseSkipToken('http://localhost:8080/subscriptions/x/resources?$top=1000')).toBeNull();
  });

  it('returns null (never throws) for null / undefined / empty', () => {
    expect(parseSkipToken(null)).toBeNull();
    expect(parseSkipToken(undefined)).toBeNull();
    expect(parseSkipToken('')).toBeNull();
  });
});

describe('parseTop', () => {
  it('parses $top as a number', () => {
    expect(
      parseTop('http://localhost:8080/subscriptions/x/resources?$top=1000&$skiptoken=abc'),
    ).toBe(1000);
  });

  it('falls back to the default when $top is absent', () => {
    expect(parseTop('http://localhost:8080/subscriptions/x/resources?$skiptoken=abc')).toBe(
      DEFAULT_TOP,
    );
  });

  it('honours an explicit fallback', () => {
    expect(parseTop('/subscriptions/x/resources', 50)).toBe(50);
  });

  it('falls back on a non-positive / non-integer $top (never trusts a bad value)', () => {
    expect(parseTop('/r?$top=0')).toBe(DEFAULT_TOP);
    expect(parseTop('/r?$top=-5')).toBe(DEFAULT_TOP);
    expect(parseTop('/r?$top=abc')).toBe(DEFAULT_TOP);
  });

  it('returns the default (never throws) for null / undefined', () => {
    expect(parseTop(null)).toBe(DEFAULT_TOP);
    expect(parseTop(undefined)).toBe(DEFAULT_TOP);
  });
});
