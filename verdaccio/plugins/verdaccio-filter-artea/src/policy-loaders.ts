import { readFileSync, statSync } from 'node:fs';
import { load as yamlLoad } from 'js-yaml';
import type { Logger } from '@verdaccio/types';
import { type CompiledPolicy, type PolicyState, compilePolicy, emptyPolicy } from './policy-compile';

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

/** Parses + compiles a raw policy document; throws on malformed YAML or invalid rules. */
function parseYamlPolicy(raw: string): CompiledPolicy {
  return compilePolicy(yamlLoad(raw));
}

/** Shared "loaded policy" success line, identical for the file and HTTP loaders. */
function logLoaded(logger: Logger, source: 'file' | 'url', location: string, policy: CompiledPolicy): void {
  logger.info(
    { [source]: location, names: policy.names.size, scopes: policy.scopes.size, ranged: policy.ranges.size },
    `filter-artea: loaded policy from @{${source}} (@{names} blocked names, @{scopes} scopes, @{ranged} ranged rules)`,
  );
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
      const policy = parseYamlPolicy(readFileSync(this.policyFile, 'utf8'));
      this.state = { ok: true, policy };
      logLoaded(this.logger, 'file', this.policyFile, policy);
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
      const policy = parseYamlPolicy(text);
      this.etag = res.headers.get('etag'); // after the parse: a bad body must be re-fetched
      this.state = { ok: true, policy };
      this.hasPolicy = true;
      this.recordSuccess();
      logLoaded(this.logger, 'url', this.url, policy);
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
  private warnedNpmMinAge = false;

  public constructor(
    private readonly npmLoader: PolicyLoader,
    private readonly upstreamLoader: PolicyLoader,
    private readonly logger: Logger,
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
    // The upstream policy source owns min_age; any min_age in the npm policy is
    // ignored here. Warn once so an operator who set it in the npm policy is not
    // surprised by a silently-different quarantine window.
    if (!this.warnedNpmMinAge && npmState.policy.minAgeMs > 0) {
      this.warnedNpmMinAge = true;
      this.logger.warn(
        {},
        'filter-artea: upstream.min_age in the npm policy is ignored; the configured upstream policy source owns min_age',
      );
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
  return upstreamLoader === null ? npmLoader : new CompositePolicyLoader(npmLoader, upstreamLoader, logger);
}
