import * as semver from 'semver';
import type { pluginUtils } from '@verdaccio/core';
import type { Logger, Manifest } from '@verdaccio/types';
import { type PolicyLoader, type PolicySourceConfig, SEMVER_OPTS, createPolicyLoader, isNameBlocked, isVersionBlocked } from './policy';
import { OsvDecisionClient } from './osv';

export interface FilterArteaConfig extends PolicySourceConfig {
  /** Registry metadata source for cold direct tarball age checks. */
  npm_registry_url?: string;
  /** policy-sync OSV decision endpoint. Omit to disable inline OSV malicious-package filtering. */
  osv_url?: string;
  /** OSV decision endpoint timeout in ms (default 5000). */
  osv_timeout_ms?: number;
}

// minimal structural express types — keeps the plugin free of an express dependency
interface HttpRequest {
  method: string;
  path: string;
}
interface HttpResponse {
  status(code: number): HttpResponse;
  json(body: unknown): void;
}
type HttpNext = () => void;
interface ExpressApp {
  use(handler: (req: HttpRequest, res: HttpResponse, next: HttpNext) => void | Promise<void>): void;
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
  private readonly osvClient: OsvDecisionClient | null;
  private readonly publishTimes = new Map<string, Map<string, number>>();

  public constructor(config: FilterArteaConfig, options: pluginUtils.PluginOptions) {
    this.logger = options.logger;
    this.policyLoader = createPolicyLoader(config, this.logger);
    this.npmRegistryUrl = (config.npm_registry_url ?? 'https://registry.npmjs.org').replace(/\/+$/, '');
    this.osvClient = config.osv_url
      ? new OsvDecisionClient(config.osv_url, this.logger, config.osv_timeout_ms)
      : null;
  }

  /** Not part of the verdaccio plugin API; lets tests/embedders stop URL polling. */
  public stop(): void {
    this.policyLoader.stop();
  }

  public async filter_metadata(metadata: Manifest): Promise<Manifest> {
    const state = this.policyLoader.current();
    const name = metadata.name;
    if (!state.ok) {
      // fail-closed. NOTE: verdaccio swallows errors thrown by filters and would
      // serve the packument UNFILTERED, so rejection = stripping every version
      this.logger.warn({ name, reason: state.reason }, 'filter-artea: rejecting @{name}: @{reason} (failing closed)');
      return this.blockAll(metadata);
    }
    const policy = state.policy;
    this.rememberPublishTimes(metadata);
    if (isNameBlocked(policy, name)) {
      this.logger.info({ name }, 'filter-artea: blocked package @{name} entirely');
      return this.blockAll(metadata);
    }
    const ranges = policy.ranges.get(name) ?? [];
    if ((!ranges.length && policy.minAgeMs <= 0 && this.osvClient === null) || !metadata.versions) {
      return metadata;
    }
    const removed = new Set(Object.keys(metadata.versions).filter((v) =>
      ranges.some((range) => semver.satisfies(v, range, SEMVER_OPTS))
        || isTooYoung(publishTimeMs(metadata, v), policy.minAgeMs),
    ));
    if (this.osvClient !== null) {
      const candidates = Object.keys(metadata.versions).filter((version) => !removed.has(version));
      const blocked = await this.osvClient.blockedVersions('npm', name, candidates);
      for (const version of blocked.keys()) {
        removed.add(version);
      }
    }
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

  /** Middleware role: runs before verdaccio's npm endpoints and guards tarball GETs. */
  public register_middlewares(app: ExpressApp, _auth: pluginUtils.IBasicAuth, _storage: unknown): void {
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
      const blocked = await this.osvClient.blockedVersions('npm', ref.name, [ref.version]);
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
    next();
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
