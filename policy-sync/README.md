# policy-sync

Syncs registry policy from the Gitea repo `${ARTEA_NAMESPACE}/registry-policy`
(or explicit `POLICY_REPO`) to the two pull-through caches:

- `npm-rules.yaml` → delivered to the Verdaccio filter plugin two ways:
  - **file (compose)**: written atomically to `$POLICY_FILE_PATH` (default
    `/policy/npm-rules.yaml` on the shared `policy-data` volume; tmp file +
    rename, mode 0644). The plugin watches the file's mtime; unchanged content
    is never rewritten, so the mtime only moves on real policy changes.
  - **HTTP (K8s, no shared volume)**: every successful sync also updates an
    in-memory copy served at `GET /policy/npm-rules.yaml`, which the plugin
    polls (`policy_url` mode). Set `POLICY_FILE_PATH=""` to skip the file write
    entirely — required in K8s where no `/policy` volume exists.
- `pypi-constraints.txt` → applied to the devpi index `root/constrained` via
  devpi's JSON HTTP API (`GET` the index config, `PATCH` it back with the
  `constraints` key replaced, Basic auth `root:$DEVPI_ROOT_PASSWORD`).
  devpi-client cannot be used: with `--outside-url` set, its `/+api` discovery
  rewrites the client's target URL to the gateway origin (see devpi/README.md).
  Replacing the whole property makes the apply idempotent; the `PATCH` is
  skipped when the fetched index config already holds the same effective
  constraints. The live config — not local state — is the comparison source,
  so a wiped devpi volume (recreated fail-closed with `*`, e2e S15) is healed
  by the next sync or poll even when the policy file did not change.

Sync triggers: once at startup, on every valid Gitea push webhook, and a poll
every 5 minutes as fallback. All triggers are coalesced into a single worker
thread, so syncs never run concurrently.

Python 3.12, stdlib only — no pip dependencies in the image at all.

The container runs the service as the non-root `policysync` user (uid 8920).
Its entrypoint starts as root only to repair the ownership of the shared
`/policy` volume (volumes created by an older root-only image stay
root-owned), then drops privileges via `setpriv`; when no `/policy` directory
exists (K8s HTTP-only mode) the repair step is skipped. The policy file itself
is written world-readable (0644) because Verdaccio reads it under a different
uid.

## Endpoints (port 8920)

| Endpoint | Description |
|----------|-------------|
| `POST /hooks/policy` | Gitea push webhook. The `X-Gitea-Signature` header (hex HMAC-SHA256 of the raw body, keyed with `POLICY_WEBHOOK_SECRET`) is verified with a constant-time compare. Valid push → `202` and a sync is scheduled. Bad/missing signature → `403`. Non-push events → `200` ignored. |
| `GET /healthz` | Always `200` while the process is up. JSON body: `status`, `last_sync_ok` (`true`/`false`/`null` before first sync finishes), `last_sync_at` (epoch seconds). |
| `GET /policy/npm-rules.yaml` | The npm policy from the last successful sync, served from memory with a **strong ETag** (quoted SHA-256 of the content) and `If-None-Match`/`304` support. Falls back to reading `$POLICY_FILE_PATH` when memory is empty (compose restart: the volume still holds the last synced file). `404` with a clear JSON body if no policy has ever been synced. **No auth**: the service is cluster-internal and the body is block rules, not secrets — never expose this port publicly. |

## Environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `POLICY_SYNC_TOKEN` | yes | — | PAT of the `svc-policy` service account (non-admin; read-only on the policy repo via the `policy-readers` team), scoped `read:repository`. Minted and rotated by `scripts/bootstrap.sh` |
| `POLICY_WEBHOOK_SECRET` | yes | — | Shared secret of the Gitea webhook on the policy repo |
| `DEVPI_ROOT_PASSWORD` | yes | — | devpi `root` password (HTTP Basic on the index PATCH) |
| `GITEA_URL` | no | `http://gitea:3000` | Gitea base URL (internal) |
| `DEVPI_URL` | no | `http://devpi:3141` | devpi base URL (internal) |
| `ARTEA_NAMESPACE` | no | `artea` | Namespace used to derive the default policy repo |
| `POLICY_REPO` | no | `${ARTEA_NAMESPACE}/registry-policy` | `owner/repo` of the policy repo |
| `POLICY_DIR` | no | `/policy` | Mount point of the shared `policy-data` volume (compose) |
| `POLICY_FILE_PATH` | no | `$POLICY_DIR/npm-rules.yaml` | Where to write the npm policy file. Set to the empty string for **HTTP-only mode** (K8s: no volume, no file write; the `GET /policy/npm-rules.yaml` endpoint is the only npm-policy output). A custom path gets its parent directory created automatically |
| `DEVPI_INDEX` | no | `root/constrained` | devpi index that receives the constraints |
| `POLICY_SYNC_PORT` | no | `8920` | HTTP listen port |
| `POLICY_SYNC_POLL_SECONDS` | no | `300` | Fallback poll interval |

## Failure behavior

- **Missing required env vars**: exits 1 immediately with a clear message
  (misconfiguration should be loud, not a silent no-op).
- **Gitea or devpi down / 5xx / network errors**: the service does not crash.
  Each sync run retries up to 5 times with exponential backoff (2s → 60s cap),
  logs every failure, then gives up until the next webhook or poll. The HTTP
  server keeps answering throughout; `/healthz` reports `last_sync_ok: false`.
- **Policy file deleted from the repo (404)**: logged as a warning and skipped;
  the previously applied policy for that ecosystem stays in effect, and the
  other file still syncs. (To clear policy, push an empty file instead of
  deleting it.)
- **devpi apply fails**: the sync is marked failed and retried, but
  `npm-rules.yaml` is still written — one ecosystem failing never blocks the
  other.
- **Half-written files**: impossible by construction; the rename is atomic and
  the tmp file lives in the same directory/filesystem.

## Development

```sh
cd policy-sync
python3.12 -m pytest          # unit tests, no network, no docker, < 2s
```
