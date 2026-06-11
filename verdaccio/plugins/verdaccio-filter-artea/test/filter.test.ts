import { mkdtempSync, rmSync, statSync, unlinkSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import FilterArtea, { parseTarballPath, type FilterArteaConfig } from '../src/index';
import { makeLogger, packument, runMiddleware } from './helpers';

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

describe('verdaccio-filter-artea', () => {
  const tmpDirs: string[] = [];

  function tmpPolicyPath(): string {
    const dir = mkdtempSync(join(tmpdir(), 'filter-artea-'));
    tmpDirs.push(dir);
    return join(dir, 'npm-rules.yaml');
  }

  afterEach(() => {
    for (const dir of tmpDirs.splice(0)) {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  describe('filter_metadata', () => {
    it('blocks a fully-denied package name (all versions removed)', async () => {
      const file = tmpPolicyPath();
      writePolicy(file, 'blocked:\n  packages:\n    - name: left-pad\n');
      const plugin = makePlugin(file);

      const output = await plugin.filter_metadata(packument('left-pad', ['1.0.0', '1.3.0']));
      expect(output.versions).toEqual({});
      expect(output['dist-tags']).toEqual({});
      expect((output.time as Record<string, string>).created).toBeDefined();
    });

    it('supports bare-string shorthand for full blocks', async () => {
      const file = tmpPolicyPath();
      writePolicy(file, 'blocked:\n  packages:\n    - left-pad\n');
      const plugin = makePlugin(file);

      const output = await plugin.filter_metadata(packument('left-pad', ['1.0.0']));
      expect(output.versions).toEqual({});
    });

    it('blocks an entire scope', async () => {
      const file = tmpPolicyPath();
      writePolicy(file, 'blocked:\n  scopes:\n    - "@evil-corp"\n');
      const plugin = makePlugin(file);

      const blocked = await plugin.filter_metadata(packument('@evil-corp/tool', ['2.0.0']));
      expect(blocked.versions).toEqual({});

      const other = packument('@good-corp/tool', ['2.0.0']);
      expect(await plugin.filter_metadata(other)).toBe(other);
    });

    it('removes only versions matching the semver range and repairs dist-tags', async () => {
      const file = tmpPolicyPath();
      writePolicy(file, ['blocked:', '  packages:', '    - name: lodash', '      versions: ">=2.0.0"', '      reason: example', ''].join('\n'));
      const plugin = makePlugin(file);

      const input = packument('lodash', ['1.0.0', '1.5.0', '2.0.0', '2.1.0-beta.1'], '2.0.0');
      const output = await plugin.filter_metadata(input);

      expect(Object.keys(output.versions)).toEqual(['1.0.0', '1.5.0']);
      expect(output['dist-tags'].latest).toBe('1.5.0'); // re-pointed off the blocked 2.0.0
      expect((output.time as Record<string, string>)['2.0.0']).toBeUndefined();
      // input packument must not be mutated
      expect(Object.keys(input.versions)).toContain('2.0.0');
      expect(input['dist-tags'].latest).toBe('2.0.0');
    });

    it('leaves non-matching packages untouched when ranges exist for other names', async () => {
      const file = tmpPolicyPath();
      writePolicy(file, 'blocked:\n  packages:\n    - name: lodash\n      versions: "<2"\n');
      const plugin = makePlugin(file);

      const input = packument('express', ['4.0.0']);
      expect(await plugin.filter_metadata(input)).toBe(input);
    });

    it('reloads the policy when the file mtime changes', async () => {
      const file = tmpPolicyPath();
      writePolicy(file, 'blocked: {}\n');
      const plugin = makePlugin(file);

      const before = packument('left-pad', ['1.3.0']);
      expect(await plugin.filter_metadata(before)).toBe(before);

      writePolicy(file, 'blocked:\n  packages:\n    - name: left-pad\n');
      const after = await plugin.filter_metadata(packument('left-pad', ['1.3.0']));
      expect(after.versions).toEqual({});
    });

    it('skips rules with invalid semver ranges instead of failing the load', async () => {
      const file = tmpPolicyPath();
      writePolicy(
        file,
        ['blocked:', '  packages:', '    - name: lodash', '      versions: "not-a-range !!"', '    - name: left-pad', ''].join('\n'),
      );
      const plugin = makePlugin(file);

      const lodash = packument('lodash', ['1.0.0']);
      expect(await plugin.filter_metadata(lodash)).toBe(lodash); // bad rule dropped
      expect((await plugin.filter_metadata(packument('left-pad', ['1.0.0']))).versions).toEqual({}); // good rule kept
    });
  });

  describe('fail-closed (default)', () => {
    it('rejects all packuments when the policy file is missing', async () => {
      const plugin = makePlugin(join(tmpdir(), 'filter-artea-does-not-exist', 'npm-rules.yaml'));
      const output = await plugin.filter_metadata(packument('left-pad', ['1.0.0', '1.3.0']));
      expect(output.versions).toEqual({});
      expect(output['dist-tags']).toEqual({});
    });

    it('rejects tarballs with 503 when the policy file is missing', () => {
      const plugin = makePlugin(join(tmpdir(), 'filter-artea-does-not-exist', 'npm-rules.yaml'));
      const result = runMiddleware(plugin, '/left-pad/-/left-pad-1.3.0.tgz');
      expect(result.status).toBe(503);
      expect(result.body!.error).toContain('policy unavailable');
      expect(result.nexted).toBe(false);
    });

    it('rejects packuments and tarballs when the policy file is malformed YAML', async () => {
      const file = tmpPolicyPath();
      writePolicy(file, 'blocked: [this is: not, valid yaml\n');
      const plugin = makePlugin(file);

      expect((await plugin.filter_metadata(packument('express', ['4.0.0']))).versions).toEqual({});
      const result = runMiddleware(plugin, '/express/-/express-4.0.0.tgz');
      expect(result.status).toBe(503);
      expect(result.body!.error).toContain('policy unavailable');
    });

    it('fails closed when a previously valid file becomes unparsable', async () => {
      const file = tmpPolicyPath();
      writePolicy(file, 'blocked:\n  packages:\n    - name: left-pad\n');
      const plugin = makePlugin(file);
      const ok = packument('express', ['4.0.0']);
      expect(await plugin.filter_metadata(ok)).toBe(ok);

      writePolicy(file, 'blocked: [this is: not, valid yaml\n');
      expect((await plugin.filter_metadata(packument('express', ['4.0.0']))).versions).toEqual({});
      expect(runMiddleware(plugin, '/express/-/express-4.0.0.tgz').status).toBe(503);
    });

    it('recovers when the file reappears (mtime reload clears the failed state)', async () => {
      const file = tmpPolicyPath();
      writePolicy(file, 'blocked:\n  packages:\n    - name: left-pad\n');
      const plugin = makePlugin(file);

      unlinkSync(file);
      expect((await plugin.filter_metadata(packument('express', ['4.0.0']))).versions).toEqual({});
      expect(runMiddleware(plugin, '/express/-/express-4.0.0.tgz').status).toBe(503);

      writePolicy(file, 'blocked:\n  packages:\n    - name: left-pad\n');
      const restored = packument('express', ['4.0.0']);
      expect(await plugin.filter_metadata(restored)).toBe(restored); // unblocked name serves again
      expect((await plugin.filter_metadata(packument('left-pad', ['1.3.0']))).versions).toEqual({}); // rules apply again
      expect(runMiddleware(plugin, '/express/-/express-4.0.0.tgz').nexted).toBe(true);
    });

    it('recovers when a malformed file is fixed', async () => {
      const file = tmpPolicyPath();
      writePolicy(file, 'blocked: [this is: not, valid yaml\n');
      const plugin = makePlugin(file);
      expect(runMiddleware(plugin, '/express/-/express-4.0.0.tgz').status).toBe(503);

      writePolicy(file, 'blocked: {}\n');
      expect(runMiddleware(plugin, '/express/-/express-4.0.0.tgz').nexted).toBe(true);
    });

    it('a stale-but-valid file keeps serving as last-known-good', async () => {
      const file = tmpPolicyPath();
      writePolicy(file, 'blocked:\n  packages:\n    - name: left-pad\n');
      const plugin = makePlugin(file);

      // nothing rewrites the file: requests keep being served from the loaded policy
      expect((await plugin.filter_metadata(packument('left-pad', ['1.3.0']))).versions).toEqual({});
      const ok = packument('express', ['4.0.0']);
      expect(await plugin.filter_metadata(ok)).toBe(ok);
    });
  });

  describe('fail_open escape hatch', () => {
    it('passes metadata through untouched when the policy file is missing', async () => {
      const plugin = makePlugin(join(tmpdir(), 'filter-artea-does-not-exist', 'npm-rules.yaml'), { fail_open: true });
      const input = packument('left-pad', ['1.0.0', '1.3.0']);
      const output = await plugin.filter_metadata(input);
      expect(output).toBe(input); // same object: no clone, no changes
    });

    it('lets tarballs through when the policy file is missing', () => {
      const plugin = makePlugin(join(tmpdir(), 'filter-artea-does-not-exist', 'npm-rules.yaml'), { fail_open: true });
      expect(runMiddleware(plugin, '/left-pad/-/left-pad-1.3.0.tgz').nexted).toBe(true);
    });

    it('keeps the last good policy when the file becomes unparsable', async () => {
      const file = tmpPolicyPath();
      writePolicy(file, 'blocked:\n  packages:\n    - name: left-pad\n');
      const plugin = makePlugin(file, { fail_open: true });
      expect((await plugin.filter_metadata(packument('left-pad', ['1.0.0']))).versions).toEqual({});

      writePolicy(file, 'blocked: [this is: not, valid yaml\n');
      expect((await plugin.filter_metadata(packument('left-pad', ['1.0.0']))).versions).toEqual({});
      const ok = packument('express', ['4.0.0']);
      expect(await plugin.filter_metadata(ok)).toBe(ok);
    });
  });

  describe('tarball middleware', () => {
    function blockedPlugin(): FilterArtea {
      const file = tmpPolicyPath();
      writePolicy(
        file,
        [
          'blocked:',
          '  scopes:',
          '    - "@evil-corp"',
          '  packages:',
          '    - name: left-pad',
          '      versions: "1.3.0"',
          '    - name: event-stream',
          '    - name: tool',
          '      versions: ">=2.0.0"',
          '',
        ].join('\n'),
      );
      return makePlugin(file);
    }

    it('rejects a blocked version with a 403 JSON error', () => {
      const result = runMiddleware(blockedPlugin(), '/left-pad/-/left-pad-1.3.0.tgz');
      expect(result.status).toBe(403);
      expect(result.body!.error).toContain('left-pad@1.3.0');
      expect(result.nexted).toBe(false);
    });

    it('lets non-blocked versions of the same (hyphenated) name through', () => {
      const result = runMiddleware(blockedPlugin(), '/left-pad/-/left-pad-1.2.0.tgz');
      expect(result.status).toBeNull();
      expect(result.nexted).toBe(true);
    });

    it('rejects every tarball of a fully-blocked name', () => {
      expect(runMiddleware(blockedPlugin(), '/event-stream/-/event-stream-3.3.6.tgz').status).toBe(403);
      // even with a filename that does not yield a version
      expect(runMiddleware(blockedPlugin(), '/event-stream/-/weird.tgz').status).toBe(403);
    });

    it('rejects blocked scopes across plain and URL-encoded path variants', () => {
      const plugin = blockedPlugin();
      expect(runMiddleware(plugin, '/@evil-corp/tool/-/tool-2.0.0.tgz').status).toBe(403);
      expect(runMiddleware(plugin, '/@evil-corp%2ftool/-/tool-2.0.0.tgz').status).toBe(403);
      expect(runMiddleware(plugin, '/@evil-corp%2Ftool/-/tool-2.0.0.tgz').status).toBe(403);
      expect(runMiddleware(plugin, '/%40evil-corp%2Ftool/-/tool-2.0.0.tgz').status).toBe(403);
      // same unscoped name outside the scope: only the >=2 range applies
      expect(runMiddleware(plugin, '/tool/-/tool-1.9.0.tgz').nexted).toBe(true);
    });

    it('blocks prerelease and build-metadata versions inside a blocked range', () => {
      const plugin = blockedPlugin();
      expect(runMiddleware(plugin, '/tool/-/tool-2.1.0-beta.1.tgz').status).toBe(403); // includePrerelease
      expect(runMiddleware(plugin, '/tool/-/tool-2.0.0%2Bbuild.5.tgz').status).toBe(403); // encoded '+'
      expect(runMiddleware(plugin, '/left-pad/-/left-pad-1.3.0.tgz/').status).toBe(403); // trailing slash
    });

    it('passes non-tarball requests through untouched', () => {
      const plugin = blockedPlugin();
      for (const path of ['/', '/-/ping', '/left-pad', '/left-pad/1.3.0', '/left-pad/-/left-pad-1.3.0.tgz/-rev/1']) {
        expect(runMiddleware(plugin, path).nexted).toBe(true);
      }
      // non-read methods are verdaccio's business (publish is denied in config)
      expect(runMiddleware(plugin, '/left-pad/-/left-pad-1.3.0.tgz', 'PUT').nexted).toBe(true);
    });
  });

  describe('parseTarballPath', () => {
    it('extracts names and versions from tarball paths', () => {
      expect(parseTarballPath('/left-pad/-/left-pad-1.3.0.tgz')).toEqual({ name: 'left-pad', version: '1.3.0' });
      expect(parseTarballPath('/@scope/pkg/-/pkg-1.0.0-rc.1.tgz')).toEqual({ name: '@scope/pkg', version: '1.0.0-rc.1' });
      expect(parseTarballPath('/@scope%2fpkg/-/pkg-1.0.0.tgz')).toEqual({ name: '@scope/pkg', version: '1.0.0' });
      // filename not derived from the package name: version is unknown
      expect(parseTarballPath('/foo/-/bar-1.0.0.tgz')).toEqual({ name: 'foo', version: null });
    });

    it('returns null for non-tarball and malformed paths', () => {
      expect(parseTarballPath('/left-pad')).toBeNull();
      expect(parseTarballPath('/left-pad/-/left-pad-1.3.0.tar.gz')).toBeNull();
      expect(parseTarballPath('/a/b/c/-/c-1.0.0.tgz')).toBeNull();
      expect(parseTarballPath('/%E0%A4%A/-/x-1.0.0.tgz')).toBeNull(); // bad escape
    });
  });
});
