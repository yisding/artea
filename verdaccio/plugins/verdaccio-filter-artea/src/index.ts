import { readFileSync, statSync } from 'node:fs';
import { load as yamlLoad } from 'js-yaml';
import * as semver from 'semver';
import type { IPluginStorageFilter, Logger, Package, PluginOptions } from '@verdaccio/types';

export interface FilterArteaConfig {
  /** Path to the policy file (default /policy/npm-rules.yaml, the shared policy volume). */
  policy_file?: string;
}

interface CompiledPolicy {
  scopes: Set<string>; // '@scope' — every package in the scope is blocked
  names: Set<string>; // full package names blocked in all versions
  ranges: Map<string, string[]>; // package name -> blocked semver ranges
}

const DEFAULT_POLICY_FILE = '/policy/npm-rules.yaml';
// includePrerelease: a blocklist must err on the side of blocking more
const SEMVER_OPTS = { includePrerelease: true, loose: true } as const;

function emptyPolicy(): CompiledPolicy {
  return { scopes: new Set(), names: new Set(), ranges: new Map() };
}

interface RawPackageRule {
  name?: unknown;
  versions?: unknown;
  reason?: unknown;
}

/** Validates and compiles the parsed YAML document. Throws on structural errors. */
function compilePolicy(doc: unknown, logger: Logger): CompiledPolicy {
  const policy = emptyPolicy();
  if (doc == null) {
    return policy; // empty file = empty policy
  }
  if (typeof doc !== 'object' || Array.isArray(doc)) {
    throw new Error('policy root must be a mapping');
  }
  const blocked = (doc as { blocked?: unknown }).blocked;
  if (blocked == null) {
    return policy;
  }
  if (typeof blocked !== 'object' || Array.isArray(blocked)) {
    throw new Error('"blocked" must be a mapping');
  }
  const { scopes, packages } = blocked as { scopes?: unknown; packages?: unknown };

  if (scopes != null) {
    if (!Array.isArray(scopes)) {
      throw new Error('"blocked.scopes" must be a list');
    }
    for (const scope of scopes) {
      if (typeof scope !== 'string' || scope.length === 0) {
        logger.warn({}, 'filter-artea: skipping non-string scope entry');
        continue;
      }
      policy.scopes.add(scope.startsWith('@') ? scope : `@${scope}`);
    }
  }

  if (packages != null) {
    if (!Array.isArray(packages)) {
      throw new Error('"blocked.packages" must be a list');
    }
    for (const entry of packages) {
      // a bare string is shorthand for blocking every version
      const rule: RawPackageRule = typeof entry === 'string' ? { name: entry } : (entry as RawPackageRule);
      if (rule == null || typeof rule.name !== 'string' || rule.name.length === 0) {
        logger.warn({}, 'filter-artea: skipping packages entry without a name');
        continue;
      }
      if (rule.versions == null) {
        policy.names.add(rule.name);
        continue;
      }
      if (typeof rule.versions !== 'string' || semver.validRange(rule.versions, SEMVER_OPTS) === null) {
        logger.warn({ name: rule.name }, 'filter-artea: skipping rule for @{name}: "versions" is not a valid semver range');
        continue;
      }
      const list = policy.ranges.get(rule.name) ?? [];
      list.push(rule.versions);
      policy.ranges.set(rule.name, list);
    }
  }
  return policy;
}

/** Drops dist-tags that point at removed versions and re-points `latest`. */
function repairDistTags(pkg: Package, removed: Set<string>): void {
  const tags = pkg['dist-tags'];
  if (!tags) {
    return;
  }
  for (const [tag, version] of Object.entries(tags)) {
    if (typeof version === 'string' && removed.has(version)) {
      delete tags[tag];
    }
  }
  if (!tags.latest) {
    const remaining = Object.keys(pkg.versions ?? {}).filter((v) => semver.valid(v, SEMVER_OPTS) !== null);
    if (remaining.length > 0) {
      tags.latest = semver.rsort(remaining, SEMVER_OPTS)[0];
    }
  }
}

export default class FilterArtea implements IPluginStorageFilter<FilterArteaConfig> {
  public version?: string;
  private readonly policyFile: string;
  private readonly logger: Logger;
  private policy: CompiledPolicy = emptyPolicy();
  private lastMtimeMs: number | null = null; // null = file absent or never seen

  public constructor(config: FilterArteaConfig, options: PluginOptions<FilterArteaConfig>) {
    this.policyFile = config.policy_file || DEFAULT_POLICY_FILE;
    this.logger = options.logger;
    this.maybeReload();
  }

  public async filter_metadata(metadata: Package): Promise<Package> {
    this.maybeReload();
    const name = metadata.name;
    if (this.policy.names.has(name) || this.isScopeBlocked(name)) {
      return this.blockAll(metadata);
    }
    const ranges = this.policy.ranges.get(name);
    if (!ranges || !metadata.versions) {
      return metadata;
    }
    const removed = Object.keys(metadata.versions).filter((v) =>
      ranges.some((range) => semver.satisfies(v, range, SEMVER_OPTS)),
    );
    if (removed.length === 0) {
      return metadata;
    }
    // never mutate the input: verdaccio shares it with its storage layer
    const clone = structuredClone(metadata);
    for (const version of removed) {
      delete clone.versions[version];
      if (clone.time) {
        delete (clone.time as Record<string, unknown>)[version];
      }
    }
    repairDistTags(clone, new Set(removed));
    this.logger.info({ name, count: removed.length }, 'filter-artea: removed @{count} blocked version(s) of @{name}');
    return clone;
  }

  private isScopeBlocked(name: string): boolean {
    if (!name.startsWith('@')) {
      return false;
    }
    const slash = name.indexOf('/');
    return slash > 0 && this.policy.scopes.has(name.slice(0, slash));
  }

  /** Blocked names keep their packument shell but lose every version, so installs fail cleanly. */
  private blockAll(metadata: Package): Package {
    const clone = structuredClone(metadata);
    clone.versions = {};
    clone['dist-tags'] = {} as Package['dist-tags'];
    if (clone.time) {
      const { created, modified } = clone.time as Record<string, string | undefined>;
      clone.time = {
        ...(created ? { created } : {}),
        ...(modified ? { modified } : {}),
      } as Package['time'];
    }
    this.logger.info({ name: metadata.name }, 'filter-artea: blocked package @{name} entirely');
    return clone;
  }

  /** Re-reads the policy file when its mtime changes; cheap stat per request. */
  private maybeReload(): void {
    let mtimeMs: number;
    try {
      mtimeMs = statSync(this.policyFile).mtimeMs;
    } catch {
      // fail-open by design (see README): missing policy file = empty policy
      if (this.lastMtimeMs !== null) {
        this.logger.warn({ file: this.policyFile }, 'filter-artea: policy file @{file} disappeared, using empty policy');
        this.policy = emptyPolicy();
        this.lastMtimeMs = null;
      }
      return;
    }
    if (mtimeMs === this.lastMtimeMs) {
      return;
    }
    try {
      const raw = readFileSync(this.policyFile, 'utf8');
      this.policy = compilePolicy(yamlLoad(raw), this.logger);
      this.logger.info(
        { file: this.policyFile, names: this.policy.names.size, scopes: this.policy.scopes.size, ranged: this.policy.ranges.size },
        'filter-artea: loaded policy from @{file} (@{names} blocked names, @{scopes} scopes, @{ranged} ranged rules)',
      );
    } catch (err) {
      // keep the last good policy; recording the mtime avoids re-parsing on every request
      this.logger.error(
        { file: this.policyFile, msg: (err as Error).message },
        'filter-artea: failed to load @{file}: @{msg}; keeping previous policy',
      );
    }
    this.lastMtimeMs = mtimeMs;
  }
}
