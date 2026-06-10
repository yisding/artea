import { mkdtempSync, rmSync, statSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { Logger, Package } from '@verdaccio/types';
import { afterEach, describe, expect, it, vi } from 'vitest';
import FilterArtea from '../src/index';

function makeLogger(): Logger {
  return {
    child: vi.fn(),
    debug: vi.fn(),
    error: vi.fn(),
    http: vi.fn(),
    trace: vi.fn(),
    warn: vi.fn(),
    info: vi.fn(),
  } as unknown as Logger;
}

function makePlugin(policyFile: string): FilterArtea {
  return new FilterArtea({ policy_file: policyFile }, { config: {}, logger: makeLogger() } as never);
}

function packument(name: string, versions: string[], latest = versions[versions.length - 1]): Package {
  const pkg: Record<string, unknown> = {
    name,
    'dist-tags': { latest },
    versions: {},
    time: { created: '2020-01-01T00:00:00.000Z', modified: '2020-01-02T00:00:00.000Z' },
  };
  for (const v of versions) {
    (pkg.versions as Record<string, unknown>)[v] = { name, version: v };
    (pkg.time as Record<string, string>)[v] = '2020-01-01T12:00:00.000Z';
  }
  return pkg as unknown as Package;
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

  it('passes metadata through untouched when the policy file is missing (fail-open)', async () => {
    const plugin = makePlugin(join(tmpdir(), 'filter-artea-does-not-exist', 'npm-rules.yaml'));
    const input = packument('left-pad', ['1.0.0', '1.3.0']);
    const output = await plugin.filter_metadata(input);
    expect(output).toBe(input); // same object: no clone, no changes
  });

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

  it('keeps the last good policy when the file becomes unparseable', async () => {
    const file = tmpPolicyPath();
    writePolicy(file, 'blocked:\n  packages:\n    - name: left-pad\n');
    const plugin = makePlugin(file);
    expect((await plugin.filter_metadata(packument('left-pad', ['1.0.0']))).versions).toEqual({});

    writePolicy(file, 'blocked: [this is: not, valid yaml\n');
    expect((await plugin.filter_metadata(packument('left-pad', ['1.0.0']))).versions).toEqual({});
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
