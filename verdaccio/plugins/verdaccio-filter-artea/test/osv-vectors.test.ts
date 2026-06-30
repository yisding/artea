import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { OsvDecisionClient } from '../src/osv';
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
  afterEach(() => vi.unstubAllGlobals());

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
});
