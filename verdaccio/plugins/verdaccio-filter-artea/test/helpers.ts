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
  nexted: boolean;
}

/** Registers the plugin middleware on a fake app and replays one request through it. */
export function runMiddleware(plugin: FilterArtea, path: string, method = 'GET'): MiddlewareResult {
  let handler: ((req: unknown, res: unknown, next: () => void) => void) | undefined;
  const app = { use: (h: typeof handler) => (handler = h) };
  plugin.register_middlewares(app as never, undefined as never, undefined as never);
  const result: MiddlewareResult = { status: null, body: undefined, nexted: false };
  const res = {
    status(code: number) {
      result.status = code;
      return this;
    },
    json(body: MiddlewareResult['body']) {
      result.body = body;
    },
  };
  handler!({ method, path }, res, () => {
    result.nexted = true;
  });
  return result;
}

export async function runMiddlewareAsync(plugin: FilterArtea, path: string, method = 'GET'): Promise<MiddlewareResult> {
  let handler: ((req: unknown, res: unknown, next: () => void) => void | Promise<void>) | undefined;
  const app = { use: (h: typeof handler) => (handler = h) };
  plugin.register_middlewares(app as never, undefined as never, undefined as never);
  const result: MiddlewareResult = { status: null, body: undefined, nexted: false };
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
