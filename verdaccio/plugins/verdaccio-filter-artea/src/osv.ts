import type { Logger } from '@verdaccio/types';

// wire shape of a single OSV decision entry (untrusted until validated in parseResponse):
// { version: string; blocked: boolean; ids: string[] }

interface OsvResponse {
  status?: unknown;
  results?: unknown;
  reason?: unknown;
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

  public async blockedVersions(ecosystem: 'npm', name: string, versions: string[]): Promise<Map<string, string[]>> {
    const unique = [...new Set(versions.filter((v) => v.length > 0))];
    if (unique.length === 0) {
      return new Map();
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
      if (body.status === 'degraded') {
        this.logger.warn(
          { name, reason: typeof body.reason === 'string' ? body.reason : 'unknown' },
          'filter-artea: OSV lookup for @{name} degraded: @{reason}',
        );
      }
      return blocked;
    } catch (e) {
      this.logger.warn(
        { name, msg: (e as Error).message },
        'filter-artea: OSV lookup for @{name} failed open: @{msg}',
      );
      return new Map();
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
