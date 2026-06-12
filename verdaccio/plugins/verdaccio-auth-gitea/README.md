# verdaccio-auth-gitea

Verdaccio auth plugin (`IPluginAuth`) that delegates authentication to Gitea, making
Gitea the single identity source for the whole stack (requirement R1).

## How it works

- `authenticate(user, password)` calls `GET {gitea_url}/api/v1/user` with HTTP Basic
  `user:password`. The password **is a Gitea personal access token** (PAT) — Gitea
  accepts Basic `user:PAT` on its API.
- The response `login` must match the supplied username (case-insensitive); a valid
  PAT presented under someone else's username is rejected.
- On success, `GET /api/v1/user/orgs` maps Gitea organization names to Verdaccio
  groups. The returned group list always starts with the username: Verdaccio's auth
  chain treats an empty groups array as a *failed* authentication, so the list must
  never be empty. Org lookup failures are non-fatal: the user authenticates with the
  groups gathered so far (at minimum the username group), with a warning logged.
  Orgs are fetched in pages of 50, following `page=` until a short page; pagination
  is capped at 20 pages (1000 orgs, logged if hit) so a misbehaving backend cannot
  stall authentication.
- Positive results are cached in memory for 30 seconds, keyed by
  `user + sha256(password)` — the PAT itself is never stored. Rejections are not
  cached. Net effect: a revoked PAT stops working comfortably within one minute
  (ARCHITECTURE.md auth model; e2e scenario S12).
- If Gitea is unreachable or errors, the callback receives a 503 — the plugin fails
  closed, never open.
- `adduser`/`changePassword` are deliberately not implemented: accounts exist only in
  Gitea, so `npm adduser` against Verdaccio fails.
- Credentials are never logged. Log lines carry the username and HTTP status codes
  only; error messages are built exclusively from status codes (covered by a test).

## Configuration (verdaccio config.yaml)

```yaml
auth:
  auth-gitea:
    gitea_url: http://gitea:3000   # falls back to $GITEA_URL, then http://gitea:3000
    cache_ttl_ms: 30000            # optional, default 30000
```

## Develop

```sh
pnpm install        # from verdaccio/plugins/
pnpm build          # tsc -> dist/ (CommonJS, what verdaccio 6 loads)
pnpm test           # vitest against an in-process mock Gitea
```

No runtime dependencies: uses Node's built-in `fetch` (Node >= 18; the verdaccio:6
image satisfies this).
