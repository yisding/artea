import { createHash } from 'node:crypto';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterEach, describe, expect, it, vi } from 'vitest';
import FilterArtea from '../src/index';
import {
  DEFAULT_FAIL_GRACE_MS,
  DEFAULT_POLL_INTERVAL_MS,
  HttpPolicyLoader,
  type HttpLoaderOptions,
  createPolicyLoader,
  isNameBlocked,
} from '../src/policy';
import { makeLogger, packument, runMiddleware } from './helpers';

const BLOCK_LEFT_PAD = 'blocked:\n  packages:\n    - name: left-pad\n';
const BLOCK_LODASH = 'blocked:\n  packages:\n    - name: lodash\n';
const MALFORMED = 'blocked: [this is: not, valid yaml\n';
const INVALID_RANGE = 'blocked:\n  packages:\n    - name: lodash\n      versions: "not-a-range !!"\n';

function etagOf(yaml: string): string {
  return `"${createHash('sha256').update(yaml).digest('hex')}"`;
}

type FailMode = 'none' | 'http500' | 'destroy' | 'malformed';

/** Local policy-sync stand-in: ETag/304 handling plus injectable failure modes. */
class MockPolicyServer {
  public requests: Array<{ ifNoneMatch: string | null }> = [];
  public failMode: FailMode = 'none';
  public url = '';
  private body = '';
  private etag = '';
  private server!: Server;

  public setPolicy(yaml: string): void {
    this.body = yaml;
    this.etag = etagOf(yaml);
  }

  public async start(): Promise<void> {
    this.server = createServer((req, res) => this.handle(req, res));
    await new Promise<void>((resolve) => this.server.listen(0, '127.0.0.1', resolve));
    const { port } = this.server.address() as AddressInfo;
    this.url = `http://127.0.0.1:${port}/policy/npm-rules.yaml`;
  }

  public async stop(): Promise<void> {
    this.server.closeAllConnections(); // undici keeps sockets alive; close() would hang
    await new Promise((resolve) => this.server.close(resolve));
  }

  private handle(req: IncomingMessage, res: ServerResponse): void {
    const inm = (req.headers['if-none-match'] as string | undefined) ?? null;
    this.requests.push({ ifNoneMatch: inm });
    switch (this.failMode) {
      case 'http500':
        res.writeHead(500).end('boom');
        return;
      case 'destroy':
        req.destroy(); // connection reset, not an HTTP error
        return;
      case 'malformed':
        res.writeHead(200, { etag: '"malformed"' }).end(MALFORMED);
        return;
      case 'none':
        break;
    }
    if (inm === this.etag) {
      res.writeHead(304, { etag: this.etag }).end();
      return;
    }
    res.writeHead(200, { etag: this.etag, 'content-type': 'application/yaml' }).end(this.body);
  }
}

describe('HttpPolicyLoader', () => {
  const servers: MockPolicyServer[] = [];
  const loaders: Array<{ stop(): void }> = [];

  afterEach(async () => {
    for (const loader of loaders.splice(0)) {
      loader.stop();
    }
    for (const server of servers.splice(0)) {
      await server.stop();
    }
  });

  async function makeServer(policy = BLOCK_LEFT_PAD): Promise<MockPolicyServer> {
    const server = new MockPolicyServer();
    server.setPolicy(policy);
    await server.start();
    servers.push(server);
    return server;
  }

  /** Huge poll interval: the timer never fires, tests drive polls + the clock themselves. */
  function makeLoader(server: MockPolicyServer, opts: Partial<HttpLoaderOptions> = {}) {
    const clock = { t: 0 };
    const loader = new HttpPolicyLoader(
      { url: server.url, pollIntervalMs: 600_000, failGraceMs: 1000, now: () => clock.t, ...opts },
      makeLogger(),
    );
    loaders.push(loader);
    return { loader, clock };
  }

  function expectBlocked(loader: HttpPolicyLoader, name: string, blocked: boolean): void {
    const state = loader.current();
    expect(state.ok).toBe(true);
    if (state.ok) {
      expect(isNameBlocked(state.policy, name)).toBe(blocked);
    }
  }

  it('loads the policy on the initial fetch (no If-None-Match yet)', async () => {
    const server = await makeServer();
    const { loader } = makeLoader(server);
    await loader.poll();

    expectBlocked(loader, 'left-pad', true);
    expectBlocked(loader, 'express', false);
    expect(server.requests[0]!.ifNoneMatch).toBeNull();
  });

  it('sends If-None-Match and keeps the active policy on 304', async () => {
    const server = await makeServer();
    const { loader } = makeLoader(server);
    await loader.poll();
    const before = loader.current();

    await loader.poll();
    expect(server.requests).toHaveLength(2);
    expect(server.requests[1]!.ifNoneMatch).toBe(etagOf(BLOCK_LEFT_PAD));
    expect(loader.current()).toBe(before); // 304 = same state object, no re-parse
  });

  it('swaps the active policy when the content changes', async () => {
    const server = await makeServer(BLOCK_LEFT_PAD);
    const { loader } = makeLoader(server);
    await loader.poll();

    server.setPolicy(BLOCK_LODASH);
    await loader.poll();
    expectBlocked(loader, 'lodash', true);
    expectBlocked(loader, 'left-pad', false);
  });

  it('keeps last-known-good through transient failures within the grace window', async () => {
    const server = await makeServer();
    const { loader, clock } = makeLoader(server);
    await loader.poll();

    server.failMode = 'http500';
    clock.t = 100;
    await loader.poll();
    expectBlocked(loader, 'left-pad', true); // still serving last-known-good

    server.failMode = 'destroy'; // network-level failure, same handling
    clock.t = 500;
    await loader.poll();
    expectBlocked(loader, 'left-pad', true);
  });

  it('fails closed once failures persist past the grace window, without waiting for a poll', async () => {
    const server = await makeServer();
    const { loader, clock } = makeLoader(server);
    await loader.poll();

    server.failMode = 'http500';
    clock.t = 100;
    await loader.poll(); // failure streak starts at t=100

    clock.t = 1099; // grace is 1000ms: one ms short
    expect(loader.current().ok).toBe(true);

    clock.t = 1100; // grace expired: current() flips even with no poll in between
    const state = loader.current();
    expect(state.ok).toBe(false);
    if (!state.ok) {
      expect(state.reason).toContain('failing for over');
    }
  });

  it('recovers automatically when the endpoint serves again', async () => {
    const server = await makeServer();
    const { loader, clock } = makeLoader(server);
    await loader.poll();
    server.failMode = 'http500';
    clock.t = 100;
    await loader.poll();
    clock.t = 2000;
    expect(loader.current().ok).toBe(false);

    server.failMode = 'none';
    await loader.poll();
    expectBlocked(loader, 'left-pad', true);
  });

  it('fails closed on cold start until the first successful fetch', async () => {
    const server = await makeServer();
    server.failMode = 'http500';
    const { loader } = makeLoader(server);
    expect(loader.current().ok).toBe(false); // before any poll finishes

    await loader.poll();
    const state = loader.current();
    expect(state.ok).toBe(false);
    if (!state.ok) {
      expect(state.reason).toContain('no policy fetched');
    }

    server.failMode = 'none';
    await loader.poll();
    expectBlocked(loader, 'left-pad', true);
  });

  it('fails closed on cold start when the body is malformed YAML', async () => {
    const server = await makeServer();
    server.failMode = 'malformed';
    const { loader } = makeLoader(server);
    await loader.poll();
    expect(loader.current().ok).toBe(false);
  });

  it('fails closed on cold start when a rule has an invalid semver range', async () => {
    const server = await makeServer(INVALID_RANGE);
    const { loader } = makeLoader(server);
    await loader.poll();
    expect(loader.current().ok).toBe(false);
  });

  it('treats malformed YAML like any failure: last-known-good, then closed, then recovery', async () => {
    const server = await makeServer();
    const { loader, clock } = makeLoader(server);
    await loader.poll();

    server.failMode = 'malformed';
    clock.t = 100;
    await loader.poll();
    expectBlocked(loader, 'left-pad', true); // within grace: last-known-good

    await loader.poll();
    // the malformed response's ETag must not be adopted, or a 304 would mask the fix
    expect(server.requests[2]!.ifNoneMatch).toBe(etagOf(BLOCK_LEFT_PAD));

    clock.t = 1200;
    expect(loader.current().ok).toBe(false); // grace expired

    server.failMode = 'none';
    server.setPolicy(BLOCK_LODASH);
    await loader.poll();
    expectBlocked(loader, 'lodash', true);
  });

  it('polls on the configured interval without test-driven polls', async () => {
    const server = await makeServer(BLOCK_LEFT_PAD);
    const { loader } = makeLoader(server, { pollIntervalMs: 25, now: undefined });
    await vi.waitFor(() => expectBlocked(loader, 'left-pad', true));

    server.setPolicy(BLOCK_LODASH);
    await vi.waitFor(() => expectBlocked(loader, 'lodash', true)); // picked up by the timer
  });
});

describe('createPolicyLoader configuration', () => {
  it('rejects configs with neither policy_file nor policy_url', () => {
    expect(() => createPolicyLoader({}, makeLogger())).toThrow(/exactly one of policy_file/);
  });

  it('rejects configs with both policy_file and policy_url', () => {
    expect(() =>
      createPolicyLoader({ policy_file: '/policy/npm-rules.yaml', policy_url: 'http://x/' }, makeLogger()),
    ).toThrow(/mutually exclusive/);
  });

  it('rejects non-positive poll/grace intervals', () => {
    expect(() => createPolicyLoader({ policy_url: 'http://x/', poll_interval_ms: 0 }, makeLogger())).toThrow(
      /poll_interval_ms/,
    );
    expect(() => createPolicyLoader({ policy_url: 'http://x/', fail_grace_ms: -1 }, makeLogger())).toThrow(
      /fail_grace_ms/,
    );
  });

  it('documents the contract defaults', () => {
    expect(DEFAULT_POLL_INTERVAL_MS).toBe(10_000);
    expect(DEFAULT_FAIL_GRACE_MS).toBe(60_000);
  });
});

describe('FilterArtea in policy_url mode', () => {
  const servers: MockPolicyServer[] = [];
  const plugins: FilterArtea[] = [];

  afterEach(async () => {
    for (const plugin of plugins.splice(0)) {
      plugin.stop();
    }
    for (const server of servers.splice(0)) {
      await server.stop();
    }
  });

  function makeUrlPlugin(server: MockPolicyServer): FilterArtea {
    const plugin = new FilterArtea(
      { policy_url: server.url, poll_interval_ms: 25 },
      { config: {}, logger: makeLogger() } as never,
    );
    plugins.push(plugin);
    return plugin;
  }

  it('filters packuments and guards tarballs from a polled policy, swapping on change', async () => {
    const server = new MockPolicyServer();
    server.setPolicy(BLOCK_LEFT_PAD);
    await server.start();
    servers.push(server);
    const plugin = makeUrlPlugin(server);

    // unblocked packument passing through untouched proves the policy is loaded
    // (cold-start fail-closed also strips, so left-pad alone would be ambiguous)
    await vi.waitFor(async () => {
      const input = packument('express', ['4.0.0']);
      expect(await plugin.filter_metadata(input)).toBe(input);
    });
    expect((await plugin.filter_metadata(packument('left-pad', ['1.3.0']))).versions).toEqual({});
    expect(runMiddleware(plugin, '/left-pad/-/left-pad-1.3.0.tgz').status).toBe(403);
    expect(runMiddleware(plugin, '/express/-/express-4.0.0.tgz').nexted).toBe(true);

    server.setPolicy(BLOCK_LODASH);
    await vi.waitFor(() => {
      expect(runMiddleware(plugin, '/lodash/-/lodash-1.0.0.tgz').status).toBe(403);
    });
    expect(runMiddleware(plugin, '/left-pad/-/left-pad-1.3.0.tgz').nexted).toBe(true); // unblocked by the swap
  });

  it('fails closed on cold start: tarballs 503, packuments stripped', async () => {
    const server = new MockPolicyServer();
    server.setPolicy(BLOCK_LEFT_PAD);
    server.failMode = 'http500';
    await server.start();
    servers.push(server);
    const plugin = makeUrlPlugin(server);

    const result = runMiddleware(plugin, '/express/-/express-4.0.0.tgz');
    expect(result.status).toBe(503);
    expect(result.body!.error).toContain('policy unavailable');
    expect((await plugin.filter_metadata(packument('express', ['4.0.0']))).versions).toEqual({});
  });

  it('rejects ambiguous configuration at startup', () => {
    const options = { config: {}, logger: makeLogger() } as never;
    expect(() => new FilterArtea({}, options)).toThrow(/exactly one/);
    expect(() => new FilterArtea({ policy_file: '/p.yaml', policy_url: 'http://x/' }, options)).toThrow(
      /mutually exclusive/,
    );
  });
});
