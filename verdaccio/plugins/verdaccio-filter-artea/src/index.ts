import * as semver from 'semver';
import type {
  IBasicAuth,
  IPluginMiddleware,
  IPluginStorageFilter,
  IStorageManager,
  Logger,
  Package,
  PluginOptions,
} from '@verdaccio/types';
import { PolicyLoader, SEMVER_OPTS, isNameBlocked, isVersionBlocked } from './policy';

export interface FilterArteaConfig {
  /** Path to the policy file (default /policy/npm-rules.yaml, the shared policy volume). */
  policy_file?: string;
  /** Escape hatch: true restores the legacy fail-open behavior (missing/broken policy = allow). */
  fail_open?: boolean;
}

const DEFAULT_POLICY_FILE = '/policy/npm-rules.yaml';

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
  use(handler: (req: HttpRequest, res: HttpResponse, next: HttpNext) => void): void;
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

/**
 * One package, two verdaccio plugin roles (wire it under both `filters:` and
 * `middlewares:` in config.yaml): the filter rewrites packuments, the middleware
 * rejects direct tarball downloads of blocked versions — metadata filtering alone
 * can be bypassed by constructing the tarball URL (e2e S13). Both roles share the
 * PolicyLoader code path.
 */
export default class FilterArtea
  implements IPluginStorageFilter<FilterArteaConfig>, IPluginMiddleware<FilterArteaConfig>
{
  public version?: string;
  private readonly logger: Logger;
  private readonly policyLoader: PolicyLoader;

  public constructor(config: FilterArteaConfig, options: PluginOptions<FilterArteaConfig>) {
    this.logger = options.logger;
    this.policyLoader = new PolicyLoader(config.policy_file || DEFAULT_POLICY_FILE, config.fail_open === true, this.logger);
  }

  public async filter_metadata(metadata: Package): Promise<Package> {
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
    const ranges = policy.ranges.get(name);
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

  /** Middleware role: runs before verdaccio's npm endpoints and guards tarball GETs. */
  public register_middlewares(
    app: ExpressApp,
    _auth: IBasicAuth<FilterArteaConfig>,
    _storage: IStorageManager<FilterArteaConfig>,
  ): void {
    app.use((req, res, next) => this.guardTarball(req, res, next));
    this.logger.info({}, 'filter-artea: tarball download guard registered');
  }

  private guardTarball(req: HttpRequest, res: HttpResponse, next: HttpNext): void {
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
    next();
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
    return clone;
  }
}
