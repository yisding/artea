# verdaccio-auth-gitea

Verdaccio auth plugin (`IPluginAuth`) that delegates authentication to Gitea, making
Gitea the single identity source for the whole stack (requirement R1).

## How it works

- `authenticate(user, password)` calls `GET {gitea_url}/api/v1/user` with HTTP Basic
  `user:password`. The password **is a Gitea personal access token** (PAT) — Gitea
  accepts Basic `user:PAT` on its API.
- The response `login` must match the supplied username (case-insensitive); a valid
  PAT presented under someone else's username is rejected.
- On success, `GET /api/v1/user/orgs` maps membership in the configured namespace
  org (`private_namespace`, `$ARTEA_NAMESPACE`, then default `artea`) to a
  Verdaccio group of the same name, and `GET /api/v1/user/teams` maps only teams
  in that org as `<namespace>/<team>` groups. Other orgs are ignored. The
  returned group list always starts with the username, but only after namespace
  membership is proven; valid Gitea users outside the org are rejected. Org
  lookup failures reject authentication, while team lookup failures are
  non-fatal after the org check passes. Verdaccio's auth chain treats an empty
  groups array as a *failed* authentication, so accepted users include at least
  the username and namespace group. Membership endpoints are fetched in pages of
  50, following `page=` until a short page; pagination is capped at 20 pages
  (1000 entries per endpoint, logged if hit) so a misbehaving backend cannot
  stall authentication.
- Positive results are cached in memory for the configured TTL, keyed by
  `user + sha256(password)` — the PAT itself is never stored. Rejections are not
  cached. Artea's Verdaccio config sets this to 30 seconds; paired with the
  gateway's 30s positive auth cache, the conservative revocation guarantee is
  still within 60 seconds (ARCHITECTURE.md auth model; e2e scenario S12).
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
    private_namespace: artea       # falls back to $ARTEA_NAMESPACE, then artea
    cache_ttl_ms: 30000            # keep aligned with the gateway auth_request cache
```

## Develop

```sh
pnpm install        # from verdaccio/plugins/
pnpm build          # tsc -> dist/ (CommonJS, what verdaccio 6 loads)
pnpm test           # vitest against an in-process mock Gitea
```

No runtime dependencies: uses Node's built-in `fetch` (Node >= 18; the verdaccio:6
image satisfies this).
