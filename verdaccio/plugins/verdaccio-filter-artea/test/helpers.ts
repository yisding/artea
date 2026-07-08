import type { Logger, Manifest } from '@verdaccio/types';
import { vi } from 'vitest';
import type FilterArtea from '../src/index';

export function makeLogger(): Logger {
  return {
    child: vi.fn(),
    debug: vi.fn(),
    error: vi.fn(),
    http: vi.fn(),
    trace: vi.fn(),
    warn: vi.fn(),
    info: vi.fn(),
  } as unknown as Logger;
}

export function packument(
  name: string,
  versions: string[],
  latest = versions[versions.length - 1],
  publishTimes: Record<string, string> = {},
): Manifest {
  const pkg: Record<string, unknown> = {
    name,
    'dist-tags': { latest },
    versions: {},
    time: { created: '2020-01-01T00:00:00.000Z', modified: '2020-01-02T00:00:00.000Z' },
  };
  for (const v of versions) {
    (pkg.versions as Record<string, unknown>)[v] = { name, version: v };
    (pkg.time as Record<string, string>)[v] = publishTimes[v] ?? '2020-01-01T12:00:00.000Z';
  }
  return pkg as unknown as Manifest;
}

export interface MiddlewareResult {
  status: number | null;
  body: { error?: string } | undefined;
  redirect: { status: number; url: string } | undefined;
  nexted: boolean;
}

/** Structural stand-in for the verdaccio Auth object handed to register_middlewares. */
export interface FakeAuth {
  apiJWTmiddleware?(): (req: { remote_user?: unknown }, res: unknown, next: () => void) => void;
  allow_access?(pkg: unknown, user: unknown, callback: (err: unknown, allowed?: boolean) => void): void;
}

/** Auth that resolves an authenticated user and grants package access. */
export function allowAllAuth(): FakeAuth {
  return {
    apiJWTmiddleware: () => (req, _res, next) => {
      req.remote_user = { name: 'dev1', groups: ['$all', '$authenticated'] };
      next();
    },
    allow_access: (_pkg, _user, callback) => callback(null, true),
  };
}

/** Auth that resolves an anonymous user and denies package access. */
export function denyAllAuth(): FakeAuth {
  return {
    apiJWTmiddleware: () => (req, _res, next) => {
      req.remote_user = { name: undefined, groups: ['$all', '$anonymous'] };
      next();
    },
    allow_access: (_pkg, _user, callback) => callback(new Error('unregistered users are not allowed to access package')),
  };
}

/** Registers the plugin middleware on a fake app and replays one request through it (auth: null = no auth surface). */
export function runMiddleware(plugin: FilterArtea, path: string, method = 'GET', auth: FakeAuth | null = allowAllAuth()): MiddlewareResult {
  let handler: ((req: unknown, res: unknown, next: () => void) => void) | undefined;
  const app = { use: (h: typeof handler) => (handler = h) };
  plugin.register_middlewares(app as never, auth as never, undefined as never);
  const result: MiddlewareResult = { status: null, body: undefined, redirect: undefined, nexted: false };
  const res = {
    status(code: number) {
      result.status = code;
      return this;
    },
    json(body: MiddlewareResult['body']) {
      result.body = body;
    },
    redirect(code: number, url: string) {
      result.status = code;
      result.redirect = { status: code, url };
    },
  };
  handler!({ method, path }, res, () => {
    result.nexted = true;
  });
  return result;
}

export async function runMiddlewareAsync(plugin: FilterArtea, path: string, method = 'GET', auth: FakeAuth | null = allowAllAuth()): Promise<MiddlewareResult> {
  let handler: ((req: unknown, res: unknown, next: () => void) => void | Promise<void>) | undefined;
  const app = { use: (h: typeof handler) => (handler = h) };
  plugin.register_middlewares(app as never, auth as never, undefined as never);
  const result: MiddlewareResult = { status: null, body: undefined, redirect: undefined, nexted: false };
  await new Promise<void>((resolve) => {
    const res = {
      status(code: number) {
        result.status = code;
        return this;
      },
      json(body: MiddlewareResult['body']) {
        result.body = body;
        resolve();
      },
      redirect(code: number, url: string) {
        result.status = code;
        result.redirect = { status: code, url };
        resolve();
      },
    };
    const maybe = handler!({ method, path }, res, () => {
      result.nexted = true;
      resolve();
    });
    if (maybe && typeof (maybe as Promise<void>).then === 'function') {
      void (maybe as Promise<void>).catch(resolve);
    }
  });
  return result;
}
