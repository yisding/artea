# Artea — agent/contributor conventions

- Read `docs/ARCHITECTURE.md` before changing anything. It is the design contract;
  the "Fixed contracts" table (ports, names, paths, env vars) is binding.
- Upstream isolation is a hard rule: never vendor or patch Gitea/Verdaccio/devpi
  source. Customization happens via config, Gitea's `custom/` overlay, plugins, and
  (only if unavoidable, with an ADR) `gitea/patches/`.
- v1 scope is npm + PyPI only. Do not implement other formats; do not preclude them.
- Directory ownership: `gitea/` (overlay+patches), `verdaccio/` (config+plugins),
  `devpi/` (image+init), `gateway/` (nginx), `policy-sync/` (service), `policy/`
  (seed policy files), `scripts/` (bootstrap), `e2e/` (scenario tests), `docs/`.
- Use Conventional Commits (`type(scope): subject`). Add an `Assisted-by:` trailer
  (e.g. `Assisted-by: Claude Code:claude-fable-5`). Never add `Co-Authored-By` or
  `Signed-off-by` trailers.
- Plain JS/TS: pnpm + vitest. Python: 3.12+, pytest, stdlib-first (justify each dep).
- Keep comments short, explain why, never narrate code. No trailing whitespace.
- Secrets and version pins live in `.env` (never committed; `.env.example` is).
- Every service must be runnable via `docker compose up` + `make bootstrap`; e2e via
  `make e2e`. Sub-2-minute e2e runtime is the target (network fetches excepted).
