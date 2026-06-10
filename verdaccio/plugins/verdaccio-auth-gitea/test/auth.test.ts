import { createServer, type Server, type IncomingMessage, type ServerResponse } from 'node:http';
import type { AddressInfo } from 'node:net';
import type { Logger } from '@verdaccio/types';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AuthGitea, { type AuthGiteaConfig } from '../src/index';

/** Minimal in-process Gitea: /api/v1/user and /api/v1/user/orgs with Basic auth. */
class MockGitea {
  public tokens = new Map<string, string>(); // user -> PAT
  public orgs = new Map<string, string[]>(); // user -> org names
  public userHits = 0;
  public failOrgs = false;
  public loginOverride: string | undefined;
  private server: Server;

  public constructor() {
    this.server = createServer((req, res) => this.handle(req, res));
  }

  public async start(): Promise<string> {
    await new Promise<void>((resolve) => this.server.listen(0, '127.0.0.1', resolve));
    const { port } = this.server.address() as AddressInfo;
    return `http://127.0.0.1:${port}`;
  }

  public async stop(): Promise<void> {
    await new Promise<void>((resolve, reject) => this.server.close((err) => (err ? reject(err) : resolve())));
  }

  private handle(req: IncomingMessage, res: ServerResponse): void {
    const match = /^Basic (.+)$/.exec(req.headers.authorization ?? '');
    const decoded = match ? Buffer.from(match[1]!, 'base64').toString() : '';
    const sep = decoded.indexOf(':');
    const user = decoded.slice(0, sep);
    const pat = decoded.slice(sep + 1);
    const authed = sep > 0 && this.tokens.get(user) === pat;

    const json = (status: number, body: unknown): void => {
      res.writeHead(status, { 'content-type': 'application/json' });
      res.end(JSON.stringify(body));
    };

    if (req.url?.startsWith('/api/v1/user/orgs')) {
      if (!authed) return json(401, { message: 'unauthorized' });
      if (this.failOrgs) return json(500, { message: 'boom' });
      return json(200, (this.orgs.get(user) ?? []).map((name) => ({ username: name })));
    }
    if (req.url === '/api/v1/user') {
      this.userHits++;
      if (!authed) return json(401, { message: 'unauthorized' });
      return json(200, { login: this.loginOverride ?? user });
    }
    json(404, { message: 'not found' });
  }
}

function makeLogger(): Logger & { calls: unknown[][] } {
  const calls: unknown[][] = [];
  const record = (...args: unknown[]) => {
    calls.push(args);
  };
  return {
    calls,
    child: vi.fn(),
    debug: record,
    error: record,
    http: record,
    trace: record,
    warn: record,
    info: record,
  } as unknown as Logger & { calls: unknown[][] };
}

function makePlugin(config: AuthGiteaConfig, logger = makeLogger()): AuthGitea {
  return new AuthGitea(config, { config: {}, logger } as never);
}

function auth(plugin: AuthGitea, user: string, pass: string): Promise<{ err: unknown; groups: string[] | false }> {
  return new Promise((resolve) => {
    plugin.authenticate(user, pass, (err, groups) => resolve({ err, groups }));
  });
}

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

describe('verdaccio-auth-gitea', () => {
  let gitea: MockGitea | undefined;

  afterEach(async () => {
    await gitea?.stop();
    gitea = undefined;
  });

  it('accepts a valid PAT and maps Gitea orgs to groups', async () => {
    gitea = new MockGitea();
    gitea.tokens.set('alice', 'pat-alice');
    gitea.orgs.set('alice', ['artea', 'platform']);
    const plugin = makePlugin({ gitea_url: await gitea.start() });

    const { err, groups } = await auth(plugin, 'alice', 'pat-alice');
    expect(err).toBeNull();
    // username leads: verdaccio rejects empty group arrays, so it must never be empty
    expect(groups).toEqual(['alice', 'artea', 'platform']);
  });

  it('rejects an invalid PAT with (null, false)', async () => {
    gitea = new MockGitea();
    gitea.tokens.set('alice', 'pat-alice');
    const plugin = makePlugin({ gitea_url: await gitea.start() });

    const { err, groups } = await auth(plugin, 'alice', 'wrong-pat');
    expect(err).toBeNull();
    expect(groups).toBe(false);
  });

  it('rejects when Gitea resolves the PAT to a different login', async () => {
    gitea = new MockGitea();
    gitea.tokens.set('alice', 'pat-alice');
    gitea.loginOverride = 'mallory'; // PAT is valid but belongs to someone else
    const plugin = makePlugin({ gitea_url: await gitea.start() });

    const { err, groups } = await auth(plugin, 'alice', 'pat-alice');
    expect(err).toBeNull();
    expect(groups).toBe(false);
  });

  it('serves repeat auths from cache, then re-validates after TTL expiry', async () => {
    gitea = new MockGitea();
    gitea.tokens.set('alice', 'pat-alice');
    gitea.orgs.set('alice', ['artea']);
    const plugin = makePlugin({ gitea_url: await gitea.start(), cache_ttl_ms: 50 });

    expect((await auth(plugin, 'alice', 'pat-alice')).groups).toEqual(['alice', 'artea']);
    expect((await auth(plugin, 'alice', 'pat-alice')).groups).toEqual(['alice', 'artea']);
    expect(gitea.userHits).toBe(1); // second call was cached

    // revoke the PAT in Gitea: still accepted until the cache entry expires
    gitea.tokens.delete('alice');
    expect((await auth(plugin, 'alice', 'pat-alice')).groups).toEqual(['alice', 'artea']);

    await sleep(80);
    const { groups } = await auth(plugin, 'alice', 'pat-alice');
    expect(groups).toBe(false);
    expect(gitea.userHits).toBe(2);
  });

  it('does not cache rejected credentials', async () => {
    gitea = new MockGitea();
    const plugin = makePlugin({ gitea_url: await gitea.start(), cache_ttl_ms: 60_000 });

    expect((await auth(plugin, 'alice', 'pat-alice')).groups).toBe(false);
    gitea.tokens.set('alice', 'pat-alice');
    expect((await auth(plugin, 'alice', 'pat-alice')).groups).toEqual(['alice']);
  });

  it('still authenticates (username group only) when the orgs endpoint fails', async () => {
    gitea = new MockGitea();
    gitea.tokens.set('alice', 'pat-alice');
    gitea.orgs.set('alice', ['artea']);
    gitea.failOrgs = true;
    const plugin = makePlugin({ gitea_url: await gitea.start() });

    const { err, groups } = await auth(plugin, 'alice', 'pat-alice');
    expect(err).toBeNull();
    expect(groups).toEqual(['alice']);
  });

  it('returns a 503 error when Gitea is unreachable', async () => {
    // nothing listens on this port
    const plugin = makePlugin({ gitea_url: 'http://127.0.0.1:1' });

    const { err, groups } = await auth(plugin, 'alice', 'pat-alice');
    expect(groups).toBe(false);
    expect((err as { statusCode?: number }).statusCode).toBe(503);
  });

  it('never logs the credential', async () => {
    gitea = new MockGitea();
    gitea.tokens.set('alice', 'super-secret-pat');
    const logger = makeLogger();
    const plugin = makePlugin({ gitea_url: await gitea.start() }, logger);

    await auth(plugin, 'alice', 'super-secret-pat'); // success path
    await auth(plugin, 'bob', 'super-secret-pat'); // rejection path
    const offline = makePlugin({ gitea_url: 'http://127.0.0.1:1' }, logger);
    await auth(offline, 'alice', 'super-secret-pat'); // backend-error path

    const logged = JSON.stringify(logger.calls);
    expect(logged).not.toContain('super-secret-pat');
    expect(logged).not.toContain(Buffer.from('alice:super-secret-pat').toString('base64'));
  });
});
