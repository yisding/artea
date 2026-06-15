import { createHash } from 'node:crypto';
import type { AuthCallback, IPluginAuth, Logger, PluginOptions } from '@verdaccio/types';

export interface AuthGiteaConfig {
  /** Base URL of the Gitea instance, e.g. http://gitea:3000 */
  gitea_url?: string;
  /** Gitea org / npm scope allowed to use Artea package proxies. */
  private_namespace?: string;
  /** TTL of the positive auth cache in milliseconds (default 30s). */
  cache_ttl_ms?: number;
}

interface CacheEntry {
  groups: string[];
  expiresAt: number;
}

const DEFAULT_GITEA_URL = 'http://gitea:3000';
// Matches gateway auth_request cache so PAT revocation clears comfortably inside S12's budget.
const DEFAULT_CACHE_TTL_MS = 30_000;
const DEFAULT_PRIVATE_NAMESPACE = 'artea';
const CACHE_SWEEP_SIZE = 1_000;
// Gitea caps page size at 50 by default; follow `page=` until a short page.
const MEMBERSHIP_PAGE_LIMIT = 50;
// Sane cap (1000 entries per endpoint) so a misbehaving backend cannot loop forever.
const MAX_MEMBERSHIP_PAGES = 20;
const NAMESPACE_PATTERN = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;

type ApiObject = Record<string, unknown>;

function nonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function objectValue(value: unknown): ApiObject | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as ApiObject) : null;
}

function appendGroup(groups: string[], seen: Set<string>, group: string | null): void {
  if (group !== null && !seen.has(group)) {
    seen.add(group);
    groups.push(group);
  }
}

function namespaceFromConfig(config: AuthGiteaConfig): string {
  const namespace = config.private_namespace ?? process.env.ARTEA_NAMESPACE ?? DEFAULT_PRIVATE_NAMESPACE;
  if (!NAMESPACE_PATTERN.test(namespace)) {
    throw new Error('auth-gitea: private_namespace must match [a-z0-9]([a-z0-9-]*[a-z0-9])?');
  }
  return namespace;
}

function teamGroupName(team: unknown, privateNamespace: string): string | null {
  const value = objectValue(team);
  if (value === null) {
    return null;
  }
  const teamName = nonEmptyString(value.name);
  if (teamName === null) {
    return null;
  }
  const organization = objectValue(value.organization) ?? objectValue(value.org);
  const orgName =
    nonEmptyString(organization?.username) ??
    nonEmptyString(organization?.name) ??
    nonEmptyString(value.org_name) ??
    nonEmptyString(value.organization_name);
  return orgName === privateNamespace ? `${privateNamespace}/${teamName}` : null;
}

/** 503 without depending on the deprecated @verdaccio/commons-api package. */
function backendUnavailable(): Error {
  const err = new Error('Gitea authentication backend unavailable');
  (err as Error & { status: number; statusCode: number }).status = 503;
  (err as Error & { status: number; statusCode: number }).statusCode = 503;
  return err;
}

export default class AuthGitea implements IPluginAuth<AuthGiteaConfig> {
  public version?: string;
  private readonly giteaUrl: string;
  private readonly privateNamespace: string;
  private readonly cacheTtlMs: number;
  private readonly logger: Logger;
  // positive results only; keyed on sha256(PAT) so the credential is never stored
  private readonly cache = new Map<string, CacheEntry>();

  public constructor(config: AuthGiteaConfig, options: PluginOptions<AuthGiteaConfig>) {
    this.giteaUrl = (config.gitea_url || process.env.GITEA_URL || DEFAULT_GITEA_URL).replace(/\/+$/, '');
    this.privateNamespace = namespaceFromConfig(config);
    this.cacheTtlMs = config.cache_ttl_ms ?? DEFAULT_CACHE_TTL_MS;
    this.logger = options.logger;
    this.logger.info(
      { url: this.giteaUrl, namespace: this.privateNamespace },
      'auth-gitea: validating credentials against @{url} for namespace @{namespace}',
    );
  }

  public authenticate(user: string, password: string, cb: AuthCallback): void {
    if (!user || !password) {
      cb(null, false);
      return;
    }
    const key = `${user}:${createHash('sha256').update(password).digest('hex')}`;
    const cached = this.cache.get(key);
    if (cached && cached.expiresAt > Date.now()) {
      cb(null, [...cached.groups]);
      return;
    }
    this.verifyAgainstGitea(user, password).then(
      (groups) => {
        if (groups === false) {
          this.cache.delete(key);
          this.logger.warn({ user }, 'auth-gitea: rejected credentials for @{user}');
          cb(null, false);
          return;
        }
        this.sweepCache();
        this.cache.set(key, { groups, expiresAt: Date.now() + this.cacheTtlMs });
        cb(null, [...groups]);
      },
      (err: Error) => {
        // err.message is built from status codes only — never from credentials
        this.logger.error({ user, msg: err.message }, 'auth-gitea: backend error for @{user}: @{msg}');
        cb(backendUnavailable() as Parameters<AuthCallback>[0], false);
      },
    );
  }

  // no adduser/changePassword on purpose: accounts exist only in Gitea (SSO), so
  // `npm adduser` against Verdaccio must fail

  /** Resolves false on bad credentials, group list on success, rejects on backend failure. */
  private async verifyAgainstGitea(user: string, password: string): Promise<string[] | false> {
    const headers = {
      // the password IS a Gitea PAT; Gitea accepts Basic user:PAT
      Authorization: `Basic ${Buffer.from(`${user}:${password}`).toString('base64')}`,
      Accept: 'application/json',
    };
    const res = await fetch(`${this.giteaUrl}/api/v1/user`, { headers });
    if (res.status === 401 || res.status === 403) {
      return false;
    }
    if (!res.ok) {
      throw new Error(`GET /api/v1/user -> HTTP ${res.status}`);
    }
    const me = (await res.json()) as { login?: unknown };
    if (typeof me.login !== 'string' || me.login.toLowerCase() !== user.toLowerCase()) {
      // valid PAT but for a different account: refuse the impersonation
      return false;
    }
    const membershipGroups = await this.fetchMembershipGroups(user, headers);
    if (!membershipGroups.includes(this.privateNamespace)) {
      this.logger.warn(
        { user, namespace: this.privateNamespace },
        'auth-gitea: @{user} is not a member of @{namespace}',
      );
      return false;
    }
    // verdaccio's auth chain treats an EMPTY groups array as a failed authentication
    // (it falls through to the chain-terminator plugin which rejects), so the
    // username always leads the group list after the org membership check passes.
    return [user, ...membershipGroups];
  }

  /** The configured namespace org and its teams become Verdaccio groups. Failures are non-fatal. */
  private async fetchMembershipGroups(user: string, headers: Record<string, string>): Promise<string[]> {
    const groups: string[] = [];
    const seen = new Set<string>();
    for (const group of await this.fetchOrgGroups(user, headers)) {
      appendGroup(groups, seen, group);
    }
    for (const group of await this.fetchTeamGroups(user, headers)) {
      appendGroup(groups, seen, group);
    }
    return groups;
  }

  private async fetchOrgGroups(user: string, headers: Record<string, string>): Promise<string[]> {
    const groups: string[] = [];
    try {
      for (let page = 1; page <= MAX_MEMBERSHIP_PAGES; page++) {
        const res = await fetch(`${this.giteaUrl}/api/v1/user/orgs?page=${page}&limit=${MEMBERSHIP_PAGE_LIMIT}`, {
          headers,
        });
        if (!res.ok) {
          this.logger.warn({ user, status: res.status }, 'auth-gitea: org lookup for @{user} failed: HTTP @{status}');
          return groups;
        }
        const orgs = (await res.json()) as Array<{ username?: unknown; name?: unknown }>;
        if (!Array.isArray(orgs)) {
          return groups;
        }
        for (const org of orgs) {
          const name = typeof org?.username === 'string' ? org.username : org?.name;
          if (name === this.privateNamespace) {
            groups.push(this.privateNamespace);
          }
        }
        if (orgs.length < MEMBERSHIP_PAGE_LIMIT) {
          return groups; // short page = last page
        }
      }
      this.logger.warn(
        { user, max: MAX_MEMBERSHIP_PAGES },
        'auth-gitea: org list for @{user} truncated after @{max} pages',
      );
      return groups;
    } catch {
      this.logger.warn({ user }, 'auth-gitea: org lookup for @{user} failed');
      return groups;
    }
  }

  private async fetchTeamGroups(user: string, headers: Record<string, string>): Promise<string[]> {
    const groups: string[] = [];
    try {
      for (let page = 1; page <= MAX_MEMBERSHIP_PAGES; page++) {
        const res = await fetch(`${this.giteaUrl}/api/v1/user/teams?page=${page}&limit=${MEMBERSHIP_PAGE_LIMIT}`, {
          headers,
        });
        if (!res.ok) {
          this.logger.warn({ user, status: res.status }, 'auth-gitea: team lookup for @{user} failed: HTTP @{status}');
          return groups;
        }
        const teams = (await res.json()) as unknown;
        if (!Array.isArray(teams)) {
          return groups;
        }
        for (const team of teams) {
          const name = teamGroupName(team, this.privateNamespace);
          if (name !== null) {
            groups.push(name);
          }
        }
        if (teams.length < MEMBERSHIP_PAGE_LIMIT) {
          return groups; // short page = last page
        }
      }
      this.logger.warn(
        { user, max: MAX_MEMBERSHIP_PAGES },
        'auth-gitea: team list for @{user} truncated after @{max} pages',
      );
      return groups;
    } catch {
      this.logger.warn({ user }, 'auth-gitea: team lookup for @{user} failed');
      return groups;
    }
  }

  private sweepCache(): void {
    if (this.cache.size < CACHE_SWEEP_SIZE) {
      return;
    }
    const now = Date.now();
    for (const [key, entry] of this.cache) {
      if (entry.expiresAt <= now) {
        this.cache.delete(key);
      }
    }
  }
}
