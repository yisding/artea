import { mkdtempSync, rmSync, statSync, writeFileSync, utimesSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import FilterArtea, { type FilterArteaConfig } from '../src/index';
import { clearOsvDecisionCacheForTests } from '../src/osv';
import { makeLogger, packument } from './helpers';

const OSV_URL = 'http://policy-sync.example/osv/querybatch';

function makePlugin(policyFile: string, extra: Omit<FilterArteaConfig, 'policy_file'> = {}): FilterArtea {
  return new FilterArtea({ policy_file: policyFile, ...extra }, { config: {}, logger: makeLogger() } as never);
}

/** Writes the policy and guarantees the mtime differs from any previous write. */
function writePolicy(file: string, content: string): void {
  let prev: number | null = null;
  try {
    prev = statSync(file).mtimeMs;
  } catch {
    // first write
  }
  writeFileSync(file, content);
  if (prev !== null && statSync(file).mtimeMs === prev) {
    const bumped = new Date(prev + 2000);
    utimesSync(file, bumped, bumped);
  }
}

/** OSV mock that blocks the given versions; counts calls so we can prove cache hits. */
function osvBlocking(blocked: string[]): ReturnType<typeof vi.fn> {
  return vi.fn(async (_url: string, init: { body?: unknown }) => {
    return {
      ok: true,
      status: 200,
      json: async () => ({
        status: 'ok',
        results: blocked.map((version) => ({
          version,
          blocked: true,
          ids: ['MAL-2026-1'],
        })),
      }),
    };
  });
}

describe('decision cache', () => {
  const tmpDirs: string[] = [];

  function tmpPolicyPath(content: string): string {
    const dir = mkdtempSync(join(tmpdir(), 'filter-artea-cache-'));
    tmpDirs.push(dir);
    const file = join(dir, 'npm-rules.yaml');
    writePolicy(file, content);
    return file;
  }

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    clearOsvDecisionCacheForTests();
    for (const dir of tmpDirs.splice(0)) {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('serves a repeat request from the cache: no extra OSV call, same result object', async () => {
    const file = tmpPolicyPath('blocked: {}\n');
    const plugin = makePlugin(file, { osv_url: OSV_URL });
    const fetchMock = osvBlocking(['1.3.0']);
    vi.stubGlobal('fetch', fetchMock);

    // two distinct input objects with identical content — the fingerprint, not
    // object identity, must drive the hit
    const first = await plugin.filter_metadata(packument('left-pad', ['1.2.0', '1.3.0'], '1.3.0'));
    const second = await plugin.filter_metadata(packument('left-pad', ['1.2.0', '1.3.0'], '1.3.0'));

    expect(fetchMock).toHaveBeenCalledTimes(1); // second request did not re-query OSV
    expect(second).toBe(first); // returned the cached decision, did not recompute/re-clone
    expect(Object.keys(second.versions)).toEqual(['1.2.0']);
  });

  it('caches a min_age decision with no OSV configured (no recompute on the repeat)', async () => {
    const file = tmpPolicyPath('upstream:\n  min_age: P3D\nblocked: {}\n');
    const plugin = makePlugin(file);
    const recent = new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString();
    const old = new Date(Date.now() - 5 * 24 * 60 * 60 * 1000).toISOString();
    const times = { '1.2.0': old, '1.3.0': recent };

    const first = await plugin.filter_metadata(packument('left-pad', ['1.2.0', '1.3.0'], '1.3.0', times));
    const second = await plugin.filter_metadata(packument('left-pad', ['1.2.0', '1.3.0'], '1.3.0', times));

    expect(second).toBe(first); // same cached clone — the version walk did not run again
    expect(Object.keys(second.versions)).toEqual(['1.2.0']); // semantics preserved
  });

  it('recomputes when the upstream version set changes (fingerprint miss)', async () => {
    const file = tmpPolicyPath('blocked: {}\n');
    const plugin = makePlugin(file, { osv_url: OSV_URL });
    const fetchMock = osvBlocking([]);
    vi.stubGlobal('fetch', fetchMock);

    await plugin.filter_metadata(packument('left-pad', ['1.0.0', '1.1.0'], '1.1.0'));
    await plugin.filter_metadata(packument('left-pad', ['1.0.0', '1.1.0', '1.2.0'], '1.2.0'));

    expect(fetchMock).toHaveBeenCalledTimes(2); // a new version invalidated the cache
  });

  it('recomputes when only the npm `modified` marker advances', async () => {
    const file = tmpPolicyPath('blocked: {}\n');
    const plugin = makePlugin(file, { osv_url: OSV_URL, osv_cache_ttl_ms: 0 });
    const fetchMock = osvBlocking([]);
    vi.stubGlobal('fetch', fetchMock);

    const a = packument('left-pad', ['1.0.0'], '1.0.0');
    const b = packument('left-pad', ['1.0.0'], '1.0.0');
    (b.time as Record<string, string>).modified = '2099-01-01T00:00:00.000Z'; // upstream republished

    await plugin.filter_metadata(a);
    await plugin.filter_metadata(b);

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('recomputes (and does not serve a stale decision) when the policy changes', async () => {
    const file = tmpPolicyPath('blocked: {}\n');
    const plugin = makePlugin(file, { osv_url: OSV_URL });
    vi.stubGlobal('fetch', osvBlocking([])); // OSV blocks nothing; the policy file drives the change

    const before = await plugin.filter_metadata(packument('lodash', ['1.0.0', '2.0.0'], '2.0.0'));
    expect(Object.keys(before.versions)).toEqual(['1.0.0', '2.0.0']); // cached: nothing removed

    writePolicy(file, 'blocked:\n  packages:\n    - name: lodash\n      versions: ">=2.0.0"\n');
    const after = await plugin.filter_metadata(packument('lodash', ['1.0.0', '2.0.0'], '2.0.0'));

    expect(Object.keys(after.versions)).toEqual(['1.0.0']); // new rule applied, not the stale cache
  });

  it('expires the cache after the TTL', async () => {
    const file = tmpPolicyPath('blocked: {}\n');
    const plugin = makePlugin(file, { osv_url: OSV_URL, decision_cache_ttl_ms: 1000, osv_cache_ttl_ms: 0 });
    const fetchMock = osvBlocking(['1.3.0']);
    vi.stubGlobal('fetch', fetchMock);
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2020-06-01T00:00:00.000Z'));

    await plugin.filter_metadata(packument('left-pad', ['1.2.0', '1.3.0'], '1.3.0'));
    await plugin.filter_metadata(packument('left-pad', ['1.2.0', '1.3.0'], '1.3.0'));
    expect(fetchMock).toHaveBeenCalledTimes(1); // still within TTL

    vi.advanceTimersByTime(1500); // past the 1000ms TTL
    await plugin.filter_metadata(packument('left-pad', ['1.2.0', '1.3.0'], '1.3.0'));
    expect(fetchMock).toHaveBeenCalledTimes(2); // recomputed after expiry
  });

  it('does not cache a fail-open OSV decision (re-queries on the next request)', async () => {
    const file = tmpPolicyPath('blocked: {}\n');
    const plugin = makePlugin(file, { osv_url: OSV_URL });
    const fetchMock = vi.fn(async () => {
      throw new Error('connect failed');
    });
    vi.stubGlobal('fetch', fetchMock);

    const input = packument('left-pad', ['1.3.0'], '1.3.0');
    expect(await plugin.filter_metadata(input)).toBe(input); // fail open: served unfiltered
    await plugin.filter_metadata(packument('left-pad', ['1.3.0'], '1.3.0'));

    expect(fetchMock).toHaveBeenCalledTimes(2); // fail-open verdict was not pinned in the cache
  });

  it.each(['degraded', 'policy_unavailable'])(
    'does not cache a %s OSV decision (re-queries on the next request)',
    async (status) => {
      const file = tmpPolicyPath('blocked: {}\n');
      const plugin = makePlugin(file, { osv_url: OSV_URL });
      const fetchMock = vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({
          status,
          reason: 'temporary outage',
          results: [{ version: '1.3.0', blocked: false, ids: [] }],
        }),
      }));
      vi.stubGlobal('fetch', fetchMock);

      await plugin.filter_metadata(packument('left-pad', ['1.3.0'], '1.3.0'));
      await plugin.filter_metadata(packument('left-pad', ['1.3.0'], '1.3.0'));

      expect(fetchMock).toHaveBeenCalledTimes(2); // non-OK verdict was not pinned in the cache
    },
  );

  it('disables caching when decision_cache_ttl_ms is 0', async () => {
    const file = tmpPolicyPath('blocked: {}\n');
    const plugin = makePlugin(file, { osv_url: OSV_URL, decision_cache_ttl_ms: 0, osv_cache_ttl_ms: 0 });
    const fetchMock = osvBlocking(['1.3.0']);
    vi.stubGlobal('fetch', fetchMock);

    await plugin.filter_metadata(packument('left-pad', ['1.2.0', '1.3.0'], '1.3.0'));
    await plugin.filter_metadata(packument('left-pad', ['1.2.0', '1.3.0'], '1.3.0'));

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('rejects an invalid decision_cache_ttl_ms at construction', () => {
    const file = tmpPolicyPath('blocked: {}\n');
    expect(() => makePlugin(file, { decision_cache_ttl_ms: -5 })).toThrow(/decision_cache_ttl_ms/);
  });
});
