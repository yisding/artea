import * as semver from 'semver';

export interface CompiledPolicy {
  scopes: Set<string>; // '@scope' — every package in the scope is blocked
  names: Set<string>; // full package names blocked in all versions
  ranges: Map<string, string[]>; // package name -> blocked semver ranges
  minAgeMs: number; // public versions younger than this are hidden/rejected
}

/** ok=false means the policy could not be loaded and callers must fail closed. */
export type PolicyState = { ok: true; policy: CompiledPolicy } | { ok: false; reason: string };

// includePrerelease: a blocklist must err on the side of blocking more
export const SEMVER_OPTS = { includePrerelease: true, loose: true } as const;

export function emptyPolicy(): CompiledPolicy {
  return { scopes: new Set(), names: new Set(), ranges: new Map(), minAgeMs: 0 };
}

interface RawPackageRule {
  name?: unknown;
  versions?: unknown;
}

const ISO_DURATION_RE = /^P(?:(\d+(?:\.\d+)?)W)?(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$/i;
const ISO_DURATION_FACTORS = [
  7 * 24 * 60 * 60 * 1000,
  24 * 60 * 60 * 1000,
  60 * 60 * 1000,
  60 * 1000,
  1000,
];

function parseIsoDurationMs(raw: string): number {
  const match = ISO_DURATION_RE.exec(raw.trim());
  if (!match || match.slice(1).every((v) => v == null)) {
    throw new Error('"upstream.min_age" must use ISO 8601 duration syntax such as "P3D" or "PT72H"');
  }
  const ms = match.slice(1).reduce((sum, value, index) => sum + (value == null ? 0 : Number(value) * ISO_DURATION_FACTORS[index]!), 0);
  if (!Number.isFinite(ms) || ms < 0) {
    throw new Error('"upstream.min_age" must be a non-negative duration');
  }
  return ms;
}

export function parseDurationMs(raw: unknown): number {
  if (raw == null || raw === 0) {
    return 0;
  }
  if (typeof raw !== 'string') {
    throw new Error('"upstream.min_age" must be an ISO 8601 duration string such as "P3D" or "PT72H"');
  }
  return parseIsoDurationMs(raw);
}

/** Validates and compiles the parsed YAML document. Throws on structural errors. */
export function compilePolicy(doc: unknown): CompiledPolicy {
  const policy = emptyPolicy();
  if (doc == null) {
    return policy; // empty file = empty policy
  }
  if (typeof doc !== 'object' || Array.isArray(doc)) {
    throw new Error('policy root must be a mapping');
  }
  const blocked = (doc as { blocked?: unknown }).blocked;
  const upstream = (doc as { upstream?: unknown }).upstream;
  if (upstream != null) {
    if (typeof upstream !== 'object' || Array.isArray(upstream)) {
      throw new Error('"upstream" must be a mapping');
    }
    const raw = (upstream as { min_age?: unknown; minimum_age?: unknown }).min_age
      ?? (upstream as { minimum_age?: unknown }).minimum_age;
    policy.minAgeMs = parseDurationMs(raw);
  }
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
        throw new Error('"blocked.scopes" entries must be non-empty strings');
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
      if (typeof entry !== 'string' && (entry === null || typeof entry !== 'object' || Array.isArray(entry))) {
        throw new Error('"blocked.packages" entries must be package names or mappings');
      }
      const rule: RawPackageRule = typeof entry === 'string' ? { name: entry } : (entry as RawPackageRule);
      if (rule == null || typeof rule.name !== 'string' || rule.name.length === 0) {
        throw new Error('"blocked.packages" entries must include a non-empty "name"');
      }
      if (rule.versions == null) {
        policy.names.add(rule.name);
        continue;
      }
      if (typeof rule.versions !== 'string' || semver.validRange(rule.versions, SEMVER_OPTS) === null) {
        throw new Error(`"blocked.packages" rule for "${rule.name}" has invalid "versions" semver range`);
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
