import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { parseDurationMs } from '../src/policy-compile';

// Shared cross-language contract (docs/policy-spec/min-age-vectors.json): the
// devpi plugin and policy-sync must parse/reject these same strings identically.
const vectors = JSON.parse(
  readFileSync(new URL('../../../../docs/policy-spec/min-age-vectors.json', import.meta.url), 'utf8'),
) as { valid: { input: string; seconds: number }[]; invalid: string[] };

describe('min_age ISO-8601 duration (shared vectors)', () => {
  for (const { input, seconds } of vectors.valid) {
    it(`parses "${input}"`, () => {
      expect(parseDurationMs(input)).toBe(seconds * 1000);
    });
  }
  for (const bad of vectors.invalid) {
    it(`rejects "${bad}"`, () => {
      expect(() => parseDurationMs(bad)).toThrow();
    });
  }
});
