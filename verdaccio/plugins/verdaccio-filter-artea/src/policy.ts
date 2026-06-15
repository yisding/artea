import { readFileSync, statSync } from 'node:fs';
import { load as yamlLoad } from 'js-yaml';
import * as semver from 'semver';
import type { Logger } from '@verdaccio/types';

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
  if (raw == null) {
    return 0;
  }
  if (raw === 0) {
    return 0;
  }
  if (typeof raw !== 'string') {
    throw new Error('"upstream.min_age" must be an ISO 8601 duration string such as "P3D" or "PT72H"');
  }
  return parseIsoDurationMs(raw);
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

/**
 * Policy source shared by the filter and middleware roles: both call current()
 * per request and act on the same PolicyState semantics, regardless of whether
 * the policy comes from a file (compose) or over HTTP (K8s).
 */
export interface PolicyLoader {
  current(): PolicyState;
  /** Stops background work (HTTP polling). No-op for the file loader. */
  stop(): void;
}

/**
 * Loads and caches the policy file — the single code path shared by the filter and
 * middleware roles. Re-reads when the mtime changes (cheap stat per request).
 * Default is fail-closed: a missing or unparsable file yields { ok: false } and
 * callers must reject; a stale-but-valid file keeps serving as last-known-good.
 */
export class FilePolicyLoader implements PolicyLoader {
  private readonly policyFile: string;
  private readonly logger: Logger;
  private state: PolicyState = { ok: true, policy: emptyPolicy() };
  private lastMtimeMs: number | null = null; // null = file absent or never seen
  private missing = false; // log the missing-file transition only once

  public constructor(policyFile: string, logger: Logger) {
    this.policyFile = policyFile;
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
        this.state = { ok: false, reason: 'policy file missing' };
        this.logger.error(
          { file: this.policyFile },
          'filter-artea: policy file @{file} is missing; failing closed until it reappears',
        );
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
      this.state = { ok: false, reason: `policy file unparsable: ${(err as Error).message}` };
      this.logger.error(
        { file: this.policyFile, msg: (err as Error).message },
        'filter-artea: failed to load @{file}: @{msg}; failing closed until it is fixed',
      );
    }
    // record the mtime either way so a broken file is not re-parsed on every request
    this.lastMtimeMs = mtimeMs;
    return this.state;
  }

  public stop(): void {
    // nothing to stop: the file loader has no background work
  }
}

export interface HttpLoaderOptions {
  url: string;
  pollIntervalMs: number;
  /** How long polls may keep failing before fail-closed kicks in. */
  failGraceMs: number;
  now?: () => number; // injectable clock so tests control the grace window
}

/**
 * Polls a policy-sync HTTP endpoint (K8s mode: no shared volume) with
 * ETag/If-None-Match. Failure semantics differ from the file loader because the
 * network makes transient failures normal:
 * - cold start with nothing fetched yet = fail closed immediately;
 * - 200 swaps the active policy, 304 keeps it;
 * - any failed refresh (network error, non-2xx, unparsable body) keeps the
 *   last-known-good policy until failures persist past failGraceMs, then fail
 *   closed; the next successful poll recovers automatically.
 */
export class HttpPolicyLoader implements PolicyLoader {
  private readonly url: string;
  private readonly failGraceMs: number;
  private readonly requestTimeoutMs: number;
  private readonly now: () => number;
  private readonly logger: Logger;
  private readonly timer: ReturnType<typeof setInterval>;
  private state: PolicyState;
  private hasPolicy = false; // at least one successful fetch+parse so far
  private etag: string | null = null; // only ever the ETag of a successfully parsed body
  private failingSince: number | null = null; // start of the current failure streak
  private closedLogged = false; // log the grace-expired transition only once
  private pending: Promise<void> | null = null; // coalesces overlapping polls

  public constructor(opts: HttpLoaderOptions, logger: Logger) {
    this.url = opts.url;
    this.failGraceMs = opts.failGraceMs;
    // a hung request must not stall polling forever, but never time out faster than a poll
    this.requestTimeoutMs = Math.max(opts.pollIntervalMs, 1000);
    this.now = opts.now ?? Date.now;
    this.logger = logger;
    this.state = { ok: false, reason: `no policy fetched from ${this.url} yet` };
    void this.poll(); // eager first fetch so a healthy endpoint opens the registry quickly
    this.timer = setInterval(() => void this.poll(), opts.pollIntervalMs);
    this.timer.unref?.(); // polling must never keep the process alive
  }

  public current(): PolicyState {
    if (
      this.hasPolicy &&
      this.failingSince !== null &&
      this.now() - this.failingSince >= this.failGraceMs
    ) {
      if (!this.closedLogged) {
        this.closedLogged = true;
        this.logger.error(
          { url: this.url, grace: this.failGraceMs },
          'filter-artea: policy refresh from @{url} failing for over @{grace}ms; failing closed until it recovers',
        );
      }
      return { ok: false, reason: `policy refresh from ${this.url} failing for over ${this.failGraceMs}ms` };
    }
    return this.state;
  }

  /** One refresh; concurrent callers share the in-flight request (also used by tests). */
  public poll(): Promise<void> {
    if (this.pending === null) {
      this.pending = this.refresh().finally(() => {
        this.pending = null;
      });
    }
    return this.pending;
  }

  public stop(): void {
    clearInterval(this.timer);
  }

  private async refresh(): Promise<void> {
    try {
      const headers: Record<string, string> = {};
      if (this.etag !== null) {
        headers['if-none-match'] = this.etag;
      }
      const res = await fetch(this.url, { headers, signal: AbortSignal.timeout(this.requestTimeoutMs) });
      if (res.status === 304) {
        this.recordSuccess();
        return;
      }
      const text = await res.text(); // always drain the body, even on errors
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const policy = compilePolicy(yamlLoad(text), this.logger);
      this.etag = res.headers.get('etag'); // after the parse: a bad body must be re-fetched
      this.state = { ok: true, policy };
      this.hasPolicy = true;
      this.recordSuccess();
      this.logger.info(
        { url: this.url, names: policy.names.size, scopes: policy.scopes.size, ranged: policy.ranges.size },
        'filter-artea: loaded policy from @{url} (@{names} blocked names, @{scopes} scopes, @{ranged} ranged rules)',
      );
    } catch (err) {
      this.recordFailure(err as Error);
    }
  }

  private recordSuccess(): void {
    if (this.failingSince !== null) {
      this.logger.info({ url: this.url }, 'filter-artea: policy refresh from @{url} recovered');
    }
    this.failingSince = null;
    this.closedLogged = false;
  }

  private recordFailure(err: Error): void {
    if (this.failingSince === null) {
      this.failingSince = this.now();
      this.logger.warn(
        { url: this.url, msg: err.message },
        'filter-artea: policy refresh from @{url} failed: @{msg}; keeping last-known-good policy',
      );
    } else {
      this.logger.debug?.(
        { url: this.url, msg: err.message },
        'filter-artea: policy refresh from @{url} still failing: @{msg}',
      );
    }
    if (!this.hasPolicy) {
      // cold start: nothing good to serve, so the reason carries the live error
      this.state = { ok: false, reason: `no policy fetched from ${this.url} yet (${err.message})` };
    }
  }
}

export class CompositePolicyLoader implements PolicyLoader {
  public constructor(
    private readonly npmLoader: PolicyLoader,
    private readonly upstreamLoader: PolicyLoader,
  ) {}

  public current(): PolicyState {
    const npmState = this.npmLoader.current();
    if (!npmState.ok) {
      return npmState;
    }
    const upstreamState = this.upstreamLoader.current();
    if (!upstreamState.ok) {
      return { ok: false, reason: `upstream policy unavailable: ${upstreamState.reason}` };
    }
    return {
      ok: true,
      policy: {
        ...npmState.policy,
        minAgeMs: upstreamState.policy.minAgeMs,
      },
    };
  }

  public stop(): void {
    this.npmLoader.stop();
    this.upstreamLoader.stop();
  }
}

export const DEFAULT_POLL_INTERVAL_MS = 10_000;
export const DEFAULT_FAIL_GRACE_MS = 60_000;

export interface PolicySourceConfig {
  policy_file?: string;
  policy_url?: string;
  upstream_policy_file?: string;
  upstream_policy_url?: string;
  poll_interval_ms?: number;
  fail_grace_ms?: number;
}

function positiveMs(key: string, value: number | undefined, fallback: number): number {
  if (value == null) {
    return fallback;
  }
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
    throw new Error(`filter-artea: ${key} must be a positive number of milliseconds`);
  }
  return value;
}

function createSinglePolicyLoader(config: PolicySourceConfig, logger: Logger, fileKey: 'policy_file' | 'upstream_policy_file', urlKey: 'policy_url' | 'upstream_policy_url'): PolicyLoader | null {
  const file = config[fileKey];
  const url = config[urlKey];
  if (file && url) {
    throw new Error(`filter-artea: ${fileKey} and ${urlKey} are mutually exclusive`);
  }
  if (!file && !url) {
    return null;
  }
  if (url) {
    return new HttpPolicyLoader(
      {
        url,
        pollIntervalMs: positiveMs('poll_interval_ms', config.poll_interval_ms, DEFAULT_POLL_INTERVAL_MS),
        failGraceMs: positiveMs('fail_grace_ms', config.fail_grace_ms, DEFAULT_FAIL_GRACE_MS),
      },
      logger,
    );
  }
  return new FilePolicyLoader(file!, logger);
}

/** Validates the config and returns the matching loader. Throws on misconfiguration. */
export function createPolicyLoader(config: PolicySourceConfig, logger: Logger): PolicyLoader {
  const npmLoader = createSinglePolicyLoader(config, logger, 'policy_file', 'policy_url');
  if (npmLoader === null) {
    throw new Error(
      'filter-artea: configure exactly one of policy_file (shared /policy volume, compose) or policy_url (policy-sync HTTP endpoint, K8s)',
    );
  }
  const upstreamLoader = createSinglePolicyLoader(config, logger, 'upstream_policy_file', 'upstream_policy_url');
  return upstreamLoader === null ? npmLoader : new CompositePolicyLoader(npmLoader, upstreamLoader);
}
