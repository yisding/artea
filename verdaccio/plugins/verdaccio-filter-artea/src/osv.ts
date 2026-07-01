import type { Logger } from '@verdaccio/types';

// wire shape of a single OSV decision entry (untrusted until validated in parseResponse):
// { version: string; blocked: boolean; ids: string[] }

interface OsvResponse {
  status?: unknown;
  results?: unknown;
  reason?: unknown;
}

interface OsvRequestBody {
  ecosystem: 'npm';
  name: string;
  versions?: string[];
  blocked_only?: true;
  package_summary?: true;
}

/**
 * Result of an OSV decision lookup. `complete` is false when the verdict cannot be
 * trusted as final — the request failed and we failed open, or the endpoint reported
 * a non-OK status such as degraded or policy_unavailable. Callers must not cache a
 * non-complete decision, or the fail-open window would outlive the outage.
 */
export interface OsvDecision {
  blocked: Map<string, string[]>;
  complete: boolean;
}

const DEFAULT_OSV_TIMEOUT_MS = 5000;
const DEFAULT_OSV_CACHE_TTL_MS = 120_000;
const SLOW_OSV_LOOKUP_MS = 500;

interface CachedOsvVerdict {
  ids: string[];
  expiresAt: number;
}

const SHARED_OSV_CACHE = new Map<string, CachedOsvVerdict>();
const SHARED_PREWARM_CACHE = new Map<string, number>();
const SHARED_PREWARM_IN_FLIGHT = new Set<string>();

export class OsvDecisionClient {
  private readonly url: string;
  private readonly timeoutMs: number;
  private readonly cacheTtlMs: number;
  private readonly logger: Logger;
  private readonly cache = SHARED_OSV_CACHE;

  public constructor(url: string, logger: Logger, timeoutMs = DEFAULT_OSV_TIMEOUT_MS, cacheTtlMs = DEFAULT_OSV_CACHE_TTL_MS) {
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw new Error('filter-artea: osv_timeout_ms must be positive');
    }
    if (!Number.isFinite(cacheTtlMs) || cacheTtlMs < 0) {
      throw new Error('filter-artea: osv_cache_ttl_ms must be non-negative');
    }
    this.url = url;
    this.timeoutMs = timeoutMs;
    this.cacheTtlMs = cacheTtlMs;
    this.logger = logger;
  }

  public async blockedVersions(ecosystem: 'npm', name: string, versions: string[]): Promise<OsvDecision> {
    const started = Date.now();
    const unique = [...new Set(versions.filter((v) => v.length > 0))];
    if (unique.length === 0) {
      return { blocked: new Map(), complete: true };
    }
    const cachedBlocked = new Map<string, string[]>();
    const misses: string[] = [];
    for (const version of unique) {
      const cached = this.cacheTtlMs > 0 ? this.cache.get(cacheKey(this.url, ecosystem, name, version)) : undefined;
      if (cached !== undefined && cached.expiresAt > Date.now()) {
        if (cached.ids.length > 0) {
          cachedBlocked.set(version, cached.ids);
        }
      } else {
        misses.push(version);
      }
    }
    if (misses.length === 0) {
      return { blocked: cachedBlocked, complete: true };
    }

    try {
      const mode = 'versioned';
      const body = await this.fetchDecision({ ecosystem, name, versions: misses, blocked_only: true });
      const status = typeof body.status === 'string' ? body.status : 'unknown';
      const fetched = parseResponse(body);
      const complete = status === 'ok';
      const elapsedMs = Date.now() - started;
      if (elapsedMs >= SLOW_OSV_LOOKUP_MS) {
        this.logger.info(
          { name, mode, status, candidates: unique.length, misses: misses.length, blocked: fetched.size, elapsedMs },
          'filter-artea: OSV lookup @{mode} for @{name} status=@{status} candidates=@{candidates} misses=@{misses} blocked=@{blocked} elapsed_ms=@{elapsedMs}',
        );
      }
      if (!complete) {
        this.logger.warn(
          { name, status, reason: typeof body.reason === 'string' ? body.reason : 'unknown' },
          'filter-artea: OSV lookup for @{name} returned @{status}: @{reason}',
        );
      }
      if (complete && this.cacheTtlMs > 0) {
        const expiresAt = Date.now() + this.cacheTtlMs;
        for (const version of misses) {
          this.cache.set(cacheKey(this.url, ecosystem, name, version), {
            ids: fetched.get(version) ?? [],
            expiresAt,
          });
        }
      }
      return { blocked: mergeBlocked(cachedBlocked, fetched), complete };
    } catch (e) {
      this.logger.warn(
        { name, msg: (e as Error).message },
        'filter-artea: OSV lookup for @{name} failed open: @{msg}',
      );
      return { blocked: cachedBlocked, complete: false };
    }
  }

  public prewarmPackages(ecosystem: 'npm', names: string[]): void {
    if (this.cacheTtlMs <= 0) {
      return;
    }
    const now = Date.now();
    const unique = [...new Set(names.filter((name) => name.length > 0))];
    for (const name of unique) {
      const key = summaryCacheKey(this.url, ecosystem, name);
      const cachedUntil = SHARED_PREWARM_CACHE.get(key);
      if ((cachedUntil !== undefined && cachedUntil > now) || SHARED_PREWARM_IN_FLIGHT.has(key)) {
        continue;
      }
      SHARED_PREWARM_IN_FLIGHT.add(key);
      void this.fetchDecision({ ecosystem, name, package_summary: true })
        .then((body) => {
          const status = typeof body.status === 'string' ? body.status : 'unknown';
          if (status !== 'ok' && status !== 'needs_versions') {
            this.logger.debug(
              { name, status, reason: typeof body.reason === 'string' ? body.reason : 'unknown' },
              'filter-artea: OSV prewarm for @{name} returned @{status}: @{reason}',
            );
          }
          SHARED_PREWARM_CACHE.set(key, Date.now() + this.cacheTtlMs);
        })
        .catch((e) => {
          this.logger.debug(
            { name, msg: (e as Error).message },
            'filter-artea: OSV prewarm for @{name} failed: @{msg}',
          );
        })
        .finally(() => {
          SHARED_PREWARM_IN_FLIGHT.delete(key);
        });
    }
  }

  private async fetchDecision(body: OsvRequestBody): Promise<OsvResponse> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await fetch(this.url, {
        method: 'POST',
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      return (await res.json()) as OsvResponse;
    } finally {
      clearTimeout(timeout);
    }
  }
}

export function clearOsvDecisionCacheForTests(): void {
  SHARED_OSV_CACHE.clear();
  SHARED_PREWARM_CACHE.clear();
  SHARED_PREWARM_IN_FLIGHT.clear();
}

function cacheKey(url: string, ecosystem: 'npm', name: string, version: string): string {
  return `${url}\0${ecosystem}\0${name}\0${version}`;
}

function summaryCacheKey(url: string, ecosystem: 'npm', name: string): string {
  return `${url}\0summary\0${ecosystem}\0${name}`;
}

function mergeBlocked(a: Map<string, string[]>, b: Map<string, string[]>): Map<string, string[]> {
  if (a.size === 0) {
    return b;
  }
  if (b.size === 0) {
    return a;
  }
  return new Map([...a, ...b]);
}

function parseResponse(body: OsvResponse): Map<string, string[]> {
  const out = new Map<string, string[]>();
  if (!Array.isArray(body.results)) {
    throw new Error('invalid OSV decision response');
  }
  for (const entry of body.results) {
    if (entry === null || typeof entry !== 'object') {
      throw new Error('invalid OSV decision entry');
    }
    const { version, blocked, ids } = entry as Record<string, unknown>;
    if (typeof version !== 'string' || typeof blocked !== 'boolean' || !Array.isArray(ids)) {
      throw new Error('invalid OSV decision entry');
    }
    if (blocked) {
      out.set(version, ids.filter((id): id is string => typeof id === 'string'));
    }
  }
  return out;
}
