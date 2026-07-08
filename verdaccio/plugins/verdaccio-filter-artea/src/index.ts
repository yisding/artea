import * as semver from 'semver';
import type { pluginUtils } from '@verdaccio/core';
import type { Logger, Manifest } from '@verdaccio/types';
import { type CompiledPolicy, type PolicyLoader, type PolicySourceConfig, SEMVER_OPTS, createPolicyLoader, isNameBlocked, isVersionBlocked } from './policy';
import { OsvDecisionClient } from './osv';

export interface FilterArteaConfig extends PolicySourceConfig {
  /** Registry metadata source for cold direct tarball age checks. */
  npm_registry_url?: string;
  /** policy-sync OSV decision endpoint. Omit to disable inline OSV malicious-package filtering. */
  osv_url?: string;
  /** OSV decision endpoint timeout in ms (default 5000). */
  osv_timeout_ms?: number;
  /** In-process per-version OSV verdict cache TTL in ms (default 120000; 0 disables). */
  osv_cache_ttl_ms?: number;
  /**
   * Redirect policy-cleared public tarball requests to npmjs after the guard runs.
   * This keeps Artea's filtering surface on metadata + tarball URLs while avoiding
   * cold proxying of public artifact bytes. Omit/false to keep Verdaccio proxying.
   */
  redirect_public_tarballs?: boolean;
  /**
   * Per-package filter-decision cache TTL in ms (default 120000; 0 disables).
   * Bounds how stale a cached min_age/OSV decision may be — see the decision cache
   * note on filter_metadata.
   */
  decision_cache_ttl_ms?: number;
}

/** Matches the npmjs uplink `maxage: 2m` in config.yaml: within that window Verdaccio
 *  hands us the same cached upstream packument, so re-deriving the decision is wasted. */
export const DEFAULT_DECISION_CACHE_TTL_MS = 120_000;
/** Hard cap so the cache can never grow without bound; oldest stored entry is evicted. */
const DECISION_CACHE_MAX_ENTRIES = 4096;
const SLOW_METADATA_FILTER_MS = 500;
const MAX_PREWARM_DEPENDENCIES = 96;
const MAX_PREWARM_VERSION_SCAN = 80;

interface DecisionCacheEntry {
  fp: string;
  result: Manifest;
  expiresAt: number;
}

// minimal structural express types — keeps the plugin free of an express dependency
interface HttpRequest {
  method: string;
  path: string;
  /** populated by verdaccio's auth middleware (apiJWTmiddleware) */
  remote_user?: unknown;
}
interface HttpResponse {
  status(code: number): HttpResponse;
  json(body: unknown): void;
  redirect?(code: number, url: string): void;
}
type HttpNext = () => void;
interface ExpressApp {
  use(handler: (req: HttpRequest, res: HttpResponse, next: HttpNext) => void | Promise<void>): void;
}

/**
 * Structural view of the runtime Auth object verdaccio hands to
 * register_middlewares. The typed pluginUtils.IBasicAuth surface omits
 * apiJWTmiddleware, but verdaccio's Auth instance provides it; both members are
 * probed before use and the redirect is skipped when either is missing.
 */
interface TarballAuth {
  apiJWTmiddleware?(): (req: HttpRequest, res: HttpResponse, next: (err?: unknown) => void) => void;
  allow_access?(
    pkg: { packageName: string; packageVersion?: string },
    user: unknown,
    callback: (err: unknown, allowed?: boolean) => void,
  ): void;
}

export interface TarballRef {
  name: string;
  version: string | null; // null when the filename does not follow `<unscoped-name>-<version>`
}

// unscoped `/{pkg}/-/{file}.tgz` and scoped `/@{scope}/{pkg}/-/{file}.tgz`, matched
// after percent-decoding; optional trailing slash because express routing is lax
const TARBALL_PATH_RE = /^\/(?:(@[^/@]+)\/)?([^/@]+)\/-\/([^/]+)\.tgz\/?$/;

/** Extracts package name + version from a tarball request path, or null if not one. */
export function parseTarballPath(rawPath: string): TarballRef | null {
  let path: string;
  try {
    // npm clients also send `%2f`/`%40`-encoded variants of scoped paths
    path = decodeURIComponent(rawPath);
  } catch {
    return null; // malformed percent-escape: not a tarball URL verdaccio would serve
  }
  const match = TARBALL_PATH_RE.exec(path);
  if (!match) {
    return null;
  }
  const scope = match[1] as string | undefined;
  const base = match[2] as string;
  const file = match[3] as string;
  const name = scope ? `${scope}/${base}` : base;
  // registry filenames are `<unscoped-name>-<version>.tgz`; names may contain
  // hyphens, so strip the exact name prefix instead of splitting on '-'
  const version = file.startsWith(`${base}-`) ? file.slice(base.length + 1) : null;
  return { name, version };
}

/** Validates the optional decision-cache TTL, falling back to the default. */
function resolveDecisionCacheTtl(raw: number | undefined): number {
  if (raw === undefined) {
    return DEFAULT_DECISION_CACHE_TTL_MS;
  }
  if (typeof raw !== 'number' || !Number.isFinite(raw) || raw < 0) {
    throw new Error('filter-artea: decision_cache_ttl_ms must be a non-negative number of milliseconds');
  }
  return raw;
}

/** The packument's per-version publish-time map (`time`), or undefined when absent. */
function timeMap(metadata: Manifest): Record<string, unknown> | undefined {
  return metadata.time as Record<string, unknown> | undefined;
}

function publishTimeMs(metadata: Manifest, version: string): number | null {
  const raw = timeMap(metadata)?.[version];
  if (typeof raw !== 'string') {
    return null;
  }
  const ms = Date.parse(raw);
  return Number.isFinite(ms) ? ms : null;
}

function isTooYoung(publishedAtMs: number | null, minAgeMs: number, nowMs = Date.now()): boolean {
  if (minAgeMs <= 0) {
    return false;
  }
  if (publishedAtMs === null) {
    return true;
  }
  return nowMs - publishedAtMs < minAgeMs;
}

function dependencyNames(metadata: Manifest): string[] {
  const versions = metadata.versions ?? {};
  const versionNames = Object.keys(versions);
  const sortedVersions = semver.rsort(
    versionNames.filter((version) => semver.valid(version, SEMVER_OPTS) !== null),
    SEMVER_OPTS,
  );
  const scanVersions = (sortedVersions.length > 0 ? sortedVersions : versionNames).slice(0, MAX_PREWARM_VERSION_SCAN);
  const names = new Set<string>();
  for (const version of scanVersions) {
    const entry = versions[version] as unknown as Record<string, unknown> | undefined;
    if (entry === undefined) {
      continue;
    }
    for (const field of ['dependencies', 'optionalDependencies', 'peerDependencies']) {
      const deps = entry[field];
      if (deps === null || typeof deps !== 'object' || Array.isArray(deps)) {
        continue;
      }
      for (const name of Object.keys(deps)) {
        if (name.length > 0 && name !== metadata.name) {
          names.add(name);
          if (names.size >= MAX_PREWARM_DEPENDENCIES) {
            return [...names];
          }
        }
      }
    }
  }
  return [...names];
}

/** Drops dist-tags that point at removed versions and re-points `latest`. */
function repairDistTags(pkg: Manifest, removed: Set<string>): void {
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

/**
 * One package, two verdaccio plugin roles (wire it under both `filters:` and
 * `middlewares:` in config.yaml): the filter rewrites packuments, the middleware
 * rejects direct tarball downloads of blocked versions — metadata filtering alone
 * can be bypassed by constructing the tarball URL (e2e S13). Both roles share the
 * PolicyLoader code path.
 */
export default class FilterArtea
  implements Pick<pluginUtils.ManifestFilter<FilterArteaConfig>, 'filter_metadata'>
{
  private readonly logger: Logger;
  private readonly policyLoader: PolicyLoader;
  private readonly npmRegistryUrl: string;
  private readonly redirectPublicTarballs: boolean;
  private auth: TarballAuth | undefined;
  private readonly osvClient: OsvDecisionClient | null;
  private readonly publishTimes = new Map<string, Map<string, number>>();
  private readonly decisionCacheTtlMs: number;
  private readonly decisionCache = new Map<string, DecisionCacheEntry>();

  public constructor(config: FilterArteaConfig, options: pluginUtils.PluginOptions) {
    this.logger = options.logger;
    this.policyLoader = createPolicyLoader(config, this.logger);
    this.npmRegistryUrl = (config.npm_registry_url ?? 'https://registry.npmjs.org').replace(/\/+$/, '');
    this.redirectPublicTarballs = config.redirect_public_tarballs === true;
    this.decisionCacheTtlMs = resolveDecisionCacheTtl(config.decision_cache_ttl_ms);
    this.osvClient = config.osv_url
      ? new OsvDecisionClient(
        config.osv_url,
        this.logger,
        config.osv_timeout_ms,
        config.osv_cache_ttl_ms ?? this.decisionCacheTtlMs,
      )
      : null;
  }

  /** Not part of the verdaccio plugin API; lets tests/embedders stop URL polling. */
  public stop(): void {
    this.policyLoader.stop();
  }

  public async filter_metadata(metadata: Manifest): Promise<Manifest> {
    const started = Date.now();
    const state = this.policyLoader.current();
    const name = metadata.name;
    if (!state.ok) {
      // fail-closed. NOTE: verdaccio swallows errors thrown by filters and would
      // serve the packument UNFILTERED, so rejection = stripping every version
      this.logger.warn({ name, reason: state.reason }, 'filter-artea: rejecting @{name}: @{reason} (failing closed)');
      return this.blockAll(metadata);
    }
    const policy = state.policy;
    if (isNameBlocked(policy, name)) {
      this.logger.info({ name }, 'filter-artea: blocked package @{name} entirely');
      return this.blockAll(metadata);
    }
    const ranges = policy.ranges.get(name) ?? [];
    if ((!ranges.length && policy.minAgeMs <= 0 && this.osvClient === null) || !metadata.versions) {
      return metadata;
    }
    // Per-package decision cache. Without it, a packument with a long version
    // history (e.g. react-dom, ~2,800 versions) is re-walked AND re-POSTed to OSV
    // in full on every request, so per-request cost scales with version count.
    // The fingerprint pins the decision to the upstream version set + the
    // package-scoped policy signal; the TTL bounds the wall-clock drift inherent
    // to min_age (a version aging past the gate) and OSV (a verdict changing).
    const fp = this.decisionFingerprint(metadata, policy, ranges);
    if (this.decisionCacheTtlMs > 0) {
      const hit = this.decisionCache.get(name);
      if (hit !== undefined && hit.fp === fp && hit.expiresAt > Date.now()) {
        return hit.result;
      }
    }
    // miss: (re)populate publish times for the tarball guard and recompute. This
    // only runs when filtering actually applies (ranges/min_age/OSV) and the cache
    // is cold/stale, so the guard always has fresh times for the live version set.
    this.rememberPublishTimes(metadata);
    this.scheduleDependencyPrewarm(metadata);
    const removed = new Set(Object.keys(metadata.versions).filter((v) =>
      ranges.some((range) => semver.satisfies(v, range, SEMVER_OPTS))
        || isTooYoung(publishTimeMs(metadata, v), policy.minAgeMs),
    ));
    let osvComplete = true;
    if (this.osvClient !== null) {
      const candidates = Object.keys(metadata.versions).filter((version) => !removed.has(version));
      const decision = await this.osvClient.blockedVersions('npm', name, candidates);
      osvComplete = decision.complete;
      for (const version of decision.blocked.keys()) {
        removed.add(version);
      }
    }
    const result = this.applyRemovals(metadata, removed, name);
    // Only cache a trustworthy decision. A failed/degraded OSV lookup failed open;
    // caching that would keep serving the fail-open verdict (and any version OSV
    // would block) for the whole TTL, outliving the outage.
    if (this.decisionCacheTtlMs > 0 && osvComplete) {
      this.storeDecision(name, fp, result);
    }
    const elapsedMs = Date.now() - started;
    if (elapsedMs >= SLOW_METADATA_FILTER_MS) {
      this.logger.info(
        {
          name,
          versions: Object.keys(metadata.versions).length,
          removed: removed.size,
          osvComplete,
          elapsedMs,
        },
        'filter-artea: filtered metadata for @{name} versions=@{versions} removed=@{removed} osv_complete=@{osvComplete} elapsed_ms=@{elapsedMs}',
      );
    }
    return result;
  }

  /** Produces the filtered packument, or returns the input unchanged when nothing is removed. */
  private applyRemovals(metadata: Manifest, removed: Set<string>, name: string): Manifest {
    if (removed.size === 0) {
      return metadata;
    }
    // never mutate the input: verdaccio shares it with its storage layer
    const clone = structuredClone(metadata);
    for (const version of removed) {
      delete clone.versions[version];
      if (clone.time) {
        delete timeMap(clone)![version];
      }
    }
    repairDistTags(clone, removed);
    this.logger.info({ name, count: removed.size }, 'filter-artea: removed @{count} blocked version(s) of @{name}');
    return clone;
  }

  /**
   * Fingerprint of the inputs that determine the filter decision: the upstream
   * version set (count + the npm `modified` marker, which advances on any packument
   * change, + `latest`) and the package-scoped policy signal (global min_age and
   * this package's blocked ranges). A change here forces a recompute; everything
   * else — clock drift, OSV-DB updates — is bounded by the cache TTL instead.
   */
  private decisionFingerprint(metadata: Manifest, policy: CompiledPolicy, ranges: string[]): string {
    const count = Object.keys(metadata.versions ?? {}).length;
    const modifiedRaw = timeMap(metadata)?.modified;
    const modified = typeof modifiedRaw === 'string' ? modifiedRaw : '';
    const latest = metadata['dist-tags']?.latest ?? '';
    return [modified, count, latest, policy.minAgeMs, ranges.join(',')].join(' ');
  }

  private storeDecision(name: string, fp: string, result: Manifest): void {
    // delete-then-set so the most recently stored key moves to the end of the Map's
    // insertion order, making the eviction below drop the least-recently-stored entry
    this.decisionCache.delete(name);
    this.decisionCache.set(name, { fp, result, expiresAt: Date.now() + this.decisionCacheTtlMs });
    if (this.decisionCache.size > DECISION_CACHE_MAX_ENTRIES) {
      const oldest = this.decisionCache.keys().next().value;
      if (oldest !== undefined) {
        this.decisionCache.delete(oldest);
      }
    }
  }

  private scheduleDependencyPrewarm(metadata: Manifest): void {
    if (this.osvClient === null || !metadata.versions) {
      return;
    }
    const names = dependencyNames(metadata);
    if (names.length > 0) {
      this.osvClient.prewarmPackages('npm', names);
    }
  }

  /** Middleware role: runs before verdaccio's npm endpoints and guards tarball GETs. */
  public register_middlewares(app: ExpressApp, auth: pluginUtils.IBasicAuth, _storage: unknown): void {
    this.auth = auth as TarballAuth | undefined;
    app.use((req, res, next) => {
      void this.guardTarball(req, res, next).catch((err) => {
        this.logger.warn({ msg: (err as Error).message }, 'filter-artea: tarball age check failed: @{msg}');
        res.status(503).json({ error: `policy unavailable: ${(err as Error).message}; registry is failing closed` });
      });
    });
    this.logger.info({}, 'filter-artea: tarball download guard registered');
  }

  private async guardTarball(req: HttpRequest, res: HttpResponse, next: HttpNext): Promise<void> {
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      return next();
    }
    const ref = parseTarballPath(req.path);
    if (ref === null) {
      return next();
    }
    const state = this.policyLoader.current();
    if (!state.ok) {
      this.logger.warn({ name: ref.name, reason: state.reason }, 'filter-artea: rejecting tarball of @{name}: @{reason} (failing closed)');
      res.status(503).json({ error: `policy unavailable: ${state.reason}; registry is failing closed` });
      return;
    }
    if (isNameBlocked(state.policy, ref.name)) {
      this.logger.info({ name: ref.name }, 'filter-artea: blocked tarball download of @{name}');
      res.status(403).json({ error: `forbidden: ${ref.name} is blocked by registry policy` });
      return;
    }
    if (ref.version !== null && isVersionBlocked(state.policy, ref.name, ref.version)) {
      this.logger.info({ name: ref.name, version: ref.version }, 'filter-artea: blocked tarball download of @{name}@@{version}');
      res.status(403).json({ error: `forbidden: ${ref.name}@${ref.version} is blocked by registry policy` });
      return;
    }
    if (ref.version !== null && this.osvClient !== null) {
      const { blocked } = await this.osvClient.blockedVersions('npm', ref.name, [ref.version]);
      const ids = blocked.get(ref.version);
      if (ids !== undefined) {
        this.logger.info({ name: ref.name, version: ref.version, ids: ids.join(',') }, 'filter-artea: blocked OSV malicious tarball download of @{name}@@{version}');
        res.status(403).json({ error: `forbidden: ${ref.name}@${ref.version} is blocked by OSV malicious-package policy` });
        return;
      }
    }
    if (state.policy.minAgeMs > 0) {
      if (ref.version === null) {
        this.logger.info({ name: ref.name }, 'filter-artea: blocked tarball download of @{name}: version unknown');
        res.status(403).json({ error: `forbidden: ${ref.name} has no parseable version and registry policy requires upstream age verification` });
        return;
      }
      const publishedAt = await this.publishTimeFor(ref.name, ref.version);
      if (isTooYoung(publishedAt, state.policy.minAgeMs)) {
        this.logger.info({ name: ref.name, version: ref.version }, 'filter-artea: blocked too-new tarball download of @{name}@@{version}');
        res.status(403).json({ error: `forbidden: ${ref.name}@${ref.version} is newer than the registry minimum upstream age` });
        return;
      }
    }
    if (this.redirectPublicTarballs && res.redirect !== undefined) {
      const redirectUrl = this.publicTarballUrl(req.path);
      if (redirectUrl !== null && (await this.tarballRedirectAllowed(req, res, ref))) {
        this.logger.info({ name: ref.name, url: redirectUrl }, 'filter-artea: redirecting policy-cleared tarball of @{name} to upstream');
        res.redirect(302, redirectUrl);
        return;
      }
    }
    next();
  }

  /**
   * This middleware runs before verdaccio's npm routes, so a redirect issued
   * here would bypass the `packages` access ACLs (e.g. `access: $authenticated`)
   * that gate the proxied tarball response. Resolve the request's remote user
   * and re-run the access check the tarball route would apply. When the auth
   * surface is unavailable or denies, the redirect is skipped and the request
   * falls through to verdaccio, which serves — and gates — the tarball itself,
   * so failure here can never widen access.
   */
  private async tarballRedirectAllowed(req: HttpRequest, res: HttpResponse, ref: TarballRef): Promise<boolean> {
    const auth = this.auth;
    if (auth == null || typeof auth.apiJWTmiddleware !== 'function' || typeof auth.allow_access !== 'function') {
      return false;
    }
    try {
      await new Promise<void>((resolve) => {
        auth.apiJWTmiddleware!()(req, res, () => resolve());
      });
      return await new Promise<boolean>((resolve) => {
        auth.allow_access!(
          { packageName: ref.name, packageVersion: ref.version ?? undefined },
          req.remote_user,
          (err, allowed) => resolve(err == null && allowed === true),
        );
      });
    } catch (e) {
      this.logger.warn(
        { name: ref.name, msg: (e as Error).message },
        'filter-artea: tarball redirect access check for @{name} failed, proxying instead: @{msg}',
      );
      return false;
    }
  }

  private publicTarballUrl(rawPath: string): string | null {
    try {
      return `${this.npmRegistryUrl}${encodeURI(decodeURIComponent(rawPath))}`;
    } catch {
      return null;
    }
  }

  private rememberPublishTimes(metadata: Manifest): void {
    const times = timeMap(metadata);
    if (!times) {
      return;
    }
    const byVersion = this.publishTimes.get(metadata.name) ?? new Map<string, number>();
    for (const [version, raw] of Object.entries(times)) {
      if (version === 'created' || version === 'modified' || typeof raw !== 'string') {
        continue;
      }
      const ms = Date.parse(raw);
      if (Number.isFinite(ms)) {
        byVersion.set(version, ms);
      }
    }
    if (byVersion.size > 0) {
      this.publishTimes.set(metadata.name, byVersion);
    }
  }

  private async publishTimeFor(name: string, version: string): Promise<number | null> {
    const cached = this.publishTimes.get(name)?.get(version);
    if (cached !== undefined) {
      return cached;
    }
    const packument = await this.fetchNpmPackument(name);
    this.rememberPublishTimes(packument);
    return this.publishTimes.get(name)?.get(version) ?? null;
  }

  private async fetchNpmPackument(name: string): Promise<Manifest> {
    const encoded = encodeURIComponent(name);
    const url = `${this.npmRegistryUrl}/${encoded}`;
    const res = await fetch(url, { headers: { accept: 'application/json' } });
    if (!res.ok) {
      throw new Error(`npm metadata lookup for ${name} returned HTTP ${res.status}`);
    }
    return (await res.json()) as Manifest;
  }

  /**
   * Blocked names keep their packument shell but lose every version, so installs
   * fail cleanly. This must zero versions and NEVER throw: Verdaccio swallows
   * filter exceptions, so a throw here would fail OPEN (serving the unfiltered
   * packument) rather than fail-closed.
   */
  private blockAll(metadata: Manifest): Manifest {
    const clone = structuredClone(metadata);
    clone.versions = {};
    clone['dist-tags'] = {} as Manifest['dist-tags'];
    if (clone.time) {
      const { created, modified } = clone.time as Record<string, string | undefined>;
      clone.time = {
        ...(created ? { created } : {}),
        ...(modified ? { modified } : {}),
      } as Manifest['time'];
    }
    return clone;
  }
}
