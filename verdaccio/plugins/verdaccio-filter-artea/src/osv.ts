import type { Logger } from '@verdaccio/types';

// wire shape of a single OSV decision entry (untrusted until validated in parseResponse):
// { version: string; blocked: boolean; ids: string[] }

interface OsvResponse {
  status?: unknown;
  results?: unknown;
  reason?: unknown;
}

/**
 * Result of an OSV decision lookup. `complete` is false when the verdict cannot be
 * trusted as final — the request failed and we failed open, or the endpoint reported
 * a degraded/partial result. Callers must not cache a non-complete decision, or the
 * fail-open window would outlive the outage (and a version OSV blocks once it recovers
 * would keep being served).
 */
export interface OsvDecision {
  blocked: Map<string, string[]>;
  complete: boolean;
}

const DEFAULT_OSV_TIMEOUT_MS = 5000;

export class OsvDecisionClient {
  private readonly url: string;
  private readonly timeoutMs: number;
  private readonly logger: Logger;

  public constructor(url: string, logger: Logger, timeoutMs = DEFAULT_OSV_TIMEOUT_MS) {
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw new Error('filter-artea: osv_timeout_ms must be positive');
    }
    this.url = url;
    this.timeoutMs = timeoutMs;
    this.logger = logger;
  }

  public async blockedVersions(ecosystem: 'npm', name: string, versions: string[]): Promise<OsvDecision> {
    const unique = [...new Set(versions.filter((v) => v.length > 0))];
    if (unique.length === 0) {
      return { blocked: new Map(), complete: true };
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await fetch(this.url, {
        method: 'POST',
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify({ ecosystem, name, versions: unique }),
        signal: controller.signal,
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const body = (await res.json()) as OsvResponse;
      const blocked = parseResponse(body);
      const degraded = body.status === 'degraded';
      if (degraded) {
        this.logger.warn(
          { name, reason: typeof body.reason === 'string' ? body.reason : 'unknown' },
          'filter-artea: OSV lookup for @{name} degraded: @{reason}',
        );
      }
      return { blocked, complete: !degraded };
    } catch (e) {
      this.logger.warn(
        { name, msg: (e as Error).message },
        'filter-artea: OSV lookup for @{name} failed open: @{msg}',
      );
      return { blocked: new Map(), complete: false };
    } finally {
      clearTimeout(timeout);
    }
  }
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
