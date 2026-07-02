import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { OsvDecisionClient, clearOsvDecisionCacheForTests, osvDecisionCacheSizesForTests } from '../src/osv';
import { makeLogger } from './helpers';

// Shared cross-language wire-shape contract (docs/policy-spec/osv-decision-vectors.json):
// policy-sync emits this response shape and the devpi plugin parses it too, so a
// field rename on any side must break CI rather than only an integration run.
const vectors = JSON.parse(
  readFileSync(new URL('../../../../docs/policy-spec/osv-decision-vectors.json', import.meta.url), 'utf8'),
) as {
  request: { ecosystem: string; name: string; versions: string[] };
  response: unknown;
  expected: { blockedVersions: string[]; blockedIds: Record<string, string[]> };
};

describe('OSV decision wire shape (shared vectors)', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    clearOsvDecisionCacheForTests();
  });

  it('parses the shared response shape into the blocked map', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, json: async () => vectors.response })),
    );
    const client = new OsvDecisionClient('http://policy-sync.example/osv/querybatch', makeLogger());

    const { blocked, complete } = await client.blockedVersions('npm', vectors.request.name, vectors.request.versions);

    expect(complete).toBe(true);
    expect([...blocked.keys()].sort()).toEqual([...vectors.expected.blockedVersions].sort());
    for (const [version, ids] of Object.entries(vectors.expected.blockedIds)) {
      expect(blocked.get(version)).toEqual(ids);
    }
  });

  it('serves repeated complete verdicts from the in-process cache', async () => {
    const fetchMock = vi.fn(async (_url: string, init: { body?: unknown }) => {
      const body = JSON.parse(String(init.body));
      expect(body).toEqual({
        ecosystem: 'npm',
        name: 'left-pad',
        versions: ['1.0.0', '2.0.0'],
        blocked_only: true,
      });
      return {
        ok: true,
        status: 200,
        json: async () => ({
          status: 'ok',
          results: [{ version: '2.0.0', blocked: true, ids: ['MAL-2026-1'] }],
        }),
      };
    });
    vi.stubGlobal('fetch', fetchMock);
    const client = new OsvDecisionClient('http://policy-sync.example/osv/querybatch', makeLogger());

    await client.blockedVersions('npm', 'left-pad', ['1.0.0', '2.0.0']);
    const second = await client.blockedVersions('npm', 'left-pad', ['2.0.0']);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(second.complete).toBe(true);
    expect(second.blocked.get('2.0.0')).toEqual(['MAL-2026-1']);
  });

  it('shares complete verdicts between client instances for the same endpoint', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        status: 'ok',
        results: [{ version: '2.0.0', blocked: true, ids: ['MAL-2026-1'] }],
      }),
    }));
    vi.stubGlobal('fetch', fetchMock);
    const first = new OsvDecisionClient('http://policy-sync.example/osv/querybatch', makeLogger());
    const second = new OsvDecisionClient('http://policy-sync.example/osv/querybatch', makeLogger());

    await first.blockedVersions('npm', 'left-pad', ['2.0.0']);
    const cached = await second.blockedVersions('npm', 'left-pad', ['2.0.0']);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(cached.blocked.get('2.0.0')).toEqual(['MAL-2026-1']);
  });

  it('requests only blocking versioned verdicts from policy-sync', async () => {
    const fetchMock = vi.fn(async (_url: string, init: { body?: unknown }) => {
      const body = JSON.parse(String(init.body));
      return {
        ok: true,
        status: 200,
        json: async () => ({
          status: 'ok',
          results: body.versions.map((version: string) => ({
            version,
            blocked: version === '2.0.0',
            ids: version === '2.0.0' ? ['MAL-2026-1'] : [],
          })),
        }),
      };
    });
    vi.stubGlobal('fetch', fetchMock);
    const client = new OsvDecisionClient('http://policy-sync.example/osv/querybatch', makeLogger());

    const result = await client.blockedVersions('npm', 'left-pad', ['1.0.0', '2.0.0']);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      ecosystem: 'npm',
      name: 'left-pad',
      versions: ['1.0.0', '2.0.0'],
      blocked_only: true,
    });
    expect(result.complete).toBe(true);
    expect(result.blocked.get('2.0.0')).toEqual(['MAL-2026-1']);
  });

  it('evicts least-recently-used version verdicts above the cache cap', async () => {
    const bodies: Array<{ versions: string[] }> = [];
    const fetchMock = vi.fn(async (_url: string, init: { body?: unknown }) => {
      const body = JSON.parse(String(init.body)) as { versions: string[] };
      bodies.push(body);
      return {
        ok: true,
        status: 200,
        json: async () => ({ status: 'ok', results: [] }),
      };
    });
    vi.stubGlobal('fetch', fetchMock);
    const client = new OsvDecisionClient('http://policy-sync.example/osv/querybatch', makeLogger(), 5000, 60_000, 2, 2);

    await client.blockedVersions('npm', 'left-pad', ['1.0.0']);
    await client.blockedVersions('npm', 'left-pad', ['2.0.0']);
    await client.blockedVersions('npm', 'left-pad', ['1.0.0']);
    await client.blockedVersions('npm', 'left-pad', ['3.0.0']);
    await client.blockedVersions('npm', 'left-pad', ['1.0.0']);
    await client.blockedVersions('npm', 'left-pad', ['2.0.0']);

    expect(osvDecisionCacheSizesForTests().verdicts).toBe(2);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(bodies.map((body) => body.versions)).toEqual([
      ['1.0.0'],
      ['2.0.0'],
      ['3.0.0'],
      ['2.0.0'],
    ]);
  });

  it('prunes expired version verdicts when new verdicts are stored', async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ status: 'ok', results: [] }),
      })),
    );
    const client = new OsvDecisionClient('http://policy-sync.example/osv/querybatch', makeLogger(), 5000, 1000, 10, 10);

    await client.blockedVersions('npm', 'left-pad', ['1.0.0', '2.0.0']);
    expect(osvDecisionCacheSizesForTests().verdicts).toBe(2);

    vi.advanceTimersByTime(1001);
    await client.blockedVersions('npm', 'left-pad', ['3.0.0']);

    expect(osvDecisionCacheSizesForTests().verdicts).toBe(1);
  });

  it('evicts least-recently-used prewarm summaries above the cache cap', async () => {
    const bodies: Array<{ name: string; package_summary?: true }> = [];
    const fetchMock = vi.fn(async (_url: string, init: { body?: unknown }) => {
      const body = JSON.parse(String(init.body)) as { name: string; package_summary?: true };
      bodies.push(body);
      return {
        ok: true,
        status: 200,
        json: async () => ({ status: 'ok', results: [] }),
      };
    });
    vi.stubGlobal('fetch', fetchMock);
    const client = new OsvDecisionClient('http://policy-sync.example/osv/querybatch', makeLogger(), 5000, 60_000, 10, 2);

    client.prewarmPackages('npm', ['dep-a']);
    await flushAsyncWork();
    expect(osvDecisionCacheSizesForTests().prewarms).toBe(1);
    client.prewarmPackages('npm', ['dep-b']);
    await flushAsyncWork();
    expect(osvDecisionCacheSizesForTests().prewarms).toBe(2);
    client.prewarmPackages('npm', ['dep-a']);
    client.prewarmPackages('npm', ['dep-c']);
    await flushAsyncWork();
    expect(fetchMock).toHaveBeenCalledTimes(3);

    expect(osvDecisionCacheSizesForTests().prewarms).toBe(2);
    client.prewarmPackages('npm', ['dep-a']);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    client.prewarmPackages('npm', ['dep-b']);
    await flushAsyncWork();
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(bodies.map((body) => body.name)).toEqual(['dep-a', 'dep-b', 'dep-c', 'dep-b']);
  });
});

function flushAsyncWork(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}
