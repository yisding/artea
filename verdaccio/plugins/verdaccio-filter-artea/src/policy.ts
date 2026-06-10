import { readFileSync, statSync } from 'node:fs';
import { load as yamlLoad } from 'js-yaml';
import * as semver from 'semver';
import type { Logger } from '@verdaccio/types';

export interface CompiledPolicy {
  scopes: Set<string>; // '@scope' — every package in the scope is blocked
  names: Set<string>; // full package names blocked in all versions
  ranges: Map<string, string[]>; // package name -> blocked semver ranges
}

/** ok=false means the policy could not be loaded and callers must fail closed. */
export type PolicyState = { ok: true; policy: CompiledPolicy } | { ok: false; reason: string };

// includePrerelease: a blocklist must err on the side of blocking more
export const SEMVER_OPTS = { includePrerelease: true, loose: true } as const;

export function emptyPolicy(): CompiledPolicy {
  return { scopes: new Set(), names: new Set(), ranges: new Map() };
}

interface RawPackageRule {
  name?: unknown;
  versions?: unknown;
  reason?: unknown;
}

/** Validates and compiles the parsed YAML document. Throws on structural errors. */
export function compilePolicy(doc: unknown, logger: Logger): CompiledPolicy {
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

/** True when every version of the package is blocked (by name or by scope). */
export function isNameBlocked(policy: CompiledPolicy, name: string): boolean {
  if (policy.names.has(name)) {
    return true;
  }
  if (!name.startsWith('@')) {
    return false;
  }
  const slash = name.indexOf('/');
  return slash > 0 && policy.scopes.has(name.slice(0, slash));
}

/** True when this specific version falls inside a blocked range for the package. */
export function isVersionBlocked(policy: CompiledPolicy, name: string, version: string): boolean {
  const ranges = policy.ranges.get(name);
  return ranges !== undefined && ranges.some((range) => semver.satisfies(version, range, SEMVER_OPTS));
}

/**
 * Loads and caches the policy file — the single code path shared by the filter and
 * middleware roles. Re-reads when the mtime changes (cheap stat per request).
 * Default is fail-closed: a missing or unparsable file yields { ok: false } and
 * callers must reject; a stale-but-valid file keeps serving as last-known-good.
 * fail_open restores the legacy behavior (missing = empty policy, unparsable = keep
 * the last good policy).
 */
export class PolicyLoader {
  private readonly policyFile: string;
  private readonly failOpen: boolean;
  private readonly logger: Logger;
  private state: PolicyState = { ok: true, policy: emptyPolicy() };
  private lastMtimeMs: number | null = null; // null = file absent or never seen
  private missing = false; // log the missing-file transition only once

  public constructor(policyFile: string, failOpen: boolean, logger: Logger) {
    this.policyFile = policyFile;
    this.failOpen = failOpen;
    this.logger = logger;
    this.current(); // eager first load so boot logs show the initial policy state
  }

  public current(): PolicyState {
    let mtimeMs: number;
    try {
      mtimeMs = statSync(this.policyFile).mtimeMs;
    } catch {
      if (!this.missing) {
        this.missing = true;
        this.lastMtimeMs = null;
        if (this.failOpen) {
          this.state = { ok: true, policy: emptyPolicy() };
          this.logger.warn(
            { file: this.policyFile },
            'filter-artea: policy file @{file} is missing; fail_open is set, serving with an EMPTY policy',
          );
        } else {
          this.state = { ok: false, reason: 'policy file missing' };
          this.logger.error(
            { file: this.policyFile },
            'filter-artea: policy file @{file} is missing; failing closed until it reappears',
          );
        }
      }
      return this.state;
    }
    this.missing = false;
    if (mtimeMs === this.lastMtimeMs) {
      return this.state;
    }
    try {
      const raw = readFileSync(this.policyFile, 'utf8');
      const policy = compilePolicy(yamlLoad(raw), this.logger);
      this.state = { ok: true, policy };
      this.logger.info(
        { file: this.policyFile, names: policy.names.size, scopes: policy.scopes.size, ranged: policy.ranges.size },
        'filter-artea: loaded policy from @{file} (@{names} blocked names, @{scopes} scopes, @{ranged} ranged rules)',
      );
    } catch (err) {
      if (this.failOpen) {
        // legacy behavior: whatever served before (last good policy or empty) stays
        this.logger.error(
          { file: this.policyFile, msg: (err as Error).message },
          'filter-artea: failed to load @{file}: @{msg}; fail_open is set, keeping previous policy',
        );
      } else {
        this.state = { ok: false, reason: `policy file unparsable: ${(err as Error).message}` };
        this.logger.error(
          { file: this.policyFile, msg: (err as Error).message },
          'filter-artea: failed to load @{file}: @{msg}; failing closed until it is fixed',
        );
      }
    }
    // record the mtime either way so a broken file is not re-parsed on every request
    this.lastMtimeMs = mtimeMs;
    return this.state;
  }
}
