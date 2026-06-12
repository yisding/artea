// Boots real verdaccio 6 in-process with our committed config template (container
// paths swapped for temp/local ones) and the built plugins, asserts the auth +
// deny contract over HTTP, then exits. Requires `pnpm build` in ../plugins first.
import { createServer, type Server as HttpServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { dump as yamlDump, load as yamlLoad } from 'js-yaml';
import { runServer } from 'verdaccio';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

const REPO_CONFIG = resolve(__dirname, '..', '..', 'config.yaml.template');
const PLUGINS_DIR = resolve(__dirname, '..', '..', 'plugins');
const TEST_NAMESPACE = 'acme';

const USER = 'alice';
const PAT = 'pat-alice-0123456789abcdef';
const basic = `Basic ${Buffer.from(`${USER}:${PAT}`).toString('base64')}`;

function mockGitea(): HttpServer {
  return createServer((req: IncomingMessage, res: ServerResponse) => {
    const json = (status: number, body: unknown): void => {
      res.writeHead(status, { 'content-type': 'application/json' });
      res.end(JSON.stringify(body));
    };
    if (req.headers.authorization !== basic) return json(401, { message: 'unauthorized' });
    if (req.url?.startsWith('/api/v1/user/orgs')) return json(200, [{ username: TEST_NAMESPACE }]);
    if (req.url === '/api/v1/user') return json(200, { login: USER });
    json(404, {});
  });
}

describe('verdaccio 6 boots with our config and plugins', () => {
  let tmp: string;
  let gitea: HttpServer;
  let verdaccio: HttpServer;
  let base: string;

  beforeAll(async () => {
    tmp = mkdtempSync(join(tmpdir(), 'artea-smoke-'));
    gitea = mockGitea();
    await new Promise<void>((r) => gitea.listen(0, '127.0.0.1', r));
    const giteaUrl = `http://127.0.0.1:${(gitea.address() as AddressInfo).port}`;

    // start from the committed template so the smoke test validates its real keys
    const rendered = readFileSync(REPO_CONFIG, 'utf8').replaceAll('__ARTEA_NAMESPACE__', TEST_NAMESPACE);
    const config = yamlLoad(rendered) as Record<string, any>;
    expect(config.url_prefix).toBe('/npm/');
    expect(config.auth['auth-gitea']).toBeDefined();
    expect(config.filters['filter-artea']).toBeDefined();
    expect(config.middlewares['filter-artea']).toBeDefined(); // tarball guard wired (S13)
    expect(config.packages[`@${TEST_NAMESPACE}/*`].proxy).toBeUndefined(); // private scope must never proxy
    expect(config.packages['**'].proxy).toBe('npmjs');

    // container paths -> local ones
    config.storage = join(tmp, 'storage');
    config.plugins = PLUGINS_DIR;
    config.auth['auth-gitea'].gitea_url = giteaUrl;
    config.filters['filter-artea'].policy_file = join(tmp, 'npm-rules.yaml');
    config.filters['filter-artea'].upstream_policy_file = join(tmp, 'upstream-policy.yaml');
    config.middlewares['filter-artea'].policy_file = join(tmp, 'npm-rules.yaml');
    config.middlewares['filter-artea'].upstream_policy_file = join(tmp, 'upstream-policy.yaml');
    writeFileSync(join(tmp, 'npm-rules.yaml'), 'blocked:\n  packages:\n    - left-pad\n');
    writeFileSync(join(tmp, 'upstream-policy.yaml'), 'upstream:\n  min_age: P0D\n');
    delete config.listen;
    // bundled audit middleware does not resolve under pnpm's isolated node_modules
    delete config.middlewares.audit;
    config.log = { type: 'stdout', format: 'json', level: 'fatal' };

    const configPath = join(tmp, 'config.yaml');
    writeFileSync(configPath, yamlDump(config));

    verdaccio = await runServer(configPath);
    await new Promise<void>((r) => verdaccio.listen(0, '127.0.0.1', r));
    base = `http://127.0.0.1:${(verdaccio.address() as AddressInfo).port}`;
  }, 60_000);

  afterAll(async () => {
    await new Promise((r) => verdaccio?.close(r));
    await new Promise((r) => gitea?.close(r));
    rmSync(tmp, { recursive: true, force: true });
  });

  it('denies anonymous metadata access', async () => {
    const res = await fetch(`${base}/lodash`);
    expect([401, 403]).toContain(res.status);
  });

  it('authenticates a Gitea PAT end-to-end (whoami)', async () => {
    const res = await fetch(`${base}/-/whoami`, { headers: { authorization: basic } });
    expect(res.status).toBe(200);
    expect(((await res.json()) as { username: string }).username).toBe(USER);
  });

  it('a bad PAT cannot pull packages (demoted to anonymous, then denied)', async () => {
    // upstream swallows auth errors and proceeds as anonymous; the package access
    // rule ($authenticated) is what rejects — assert that, not a 401 on whoami
    const bad = `Basic ${Buffer.from(`${USER}:wrong`).toString('base64')}`;
    const res = await fetch(`${base}/lodash`, { headers: { authorization: bad } });
    expect([401, 403]).toContain(res.status);
    const who = await fetch(`${base}/-/whoami`, { headers: { authorization: bad } });
    expect(((await who.json()) as { username?: string }).username).toBeUndefined();
  });

  it('denies the configured private scope even when authenticated', async () => {
    const res = await fetch(`${base}/@${TEST_NAMESPACE}%2fhello-${TEST_NAMESPACE}`, { headers: { authorization: basic } });
    expect(res.status).toBe(403);
  });

  it('denies publish for everyone (read-only cache)', async () => {
    const res = await fetch(`${base}/some-public-package`, {
      method: 'PUT',
      headers: { authorization: basic, 'content-type': 'application/json' },
      body: JSON.stringify({ name: 'some-public-package', versions: {} }),
    });
    expect(res.status).toBe(403);
  });

  it('blocks a direct tarball download of a blocked package with 403 (S13)', async () => {
    const res = await fetch(`${base}/left-pad/-/left-pad-1.3.0.tgz`, { headers: { authorization: basic } });
    expect(res.status).toBe(403);
    expect(((await res.json()) as { error: string }).error).toContain('blocked');
  });

  it('fails closed (503) while the policy file is missing, then recovers (S15)', async () => {
    rmSync(join(tmp, 'npm-rules.yaml'));
    const gone = await fetch(`${base}/left-pad/-/left-pad-1.3.0.tgz`, { headers: { authorization: basic } });
    expect(gone.status).toBe(503);
    expect(((await gone.json()) as { error: string }).error).toContain('policy unavailable');

    writeFileSync(join(tmp, 'npm-rules.yaml'), 'blocked:\n  packages:\n    - left-pad\n');
    const back = await fetch(`${base}/left-pad/-/left-pad-1.3.0.tgz`, { headers: { authorization: basic } });
    expect(back.status).toBe(403); // policy applies again, no restart needed
  });
});
