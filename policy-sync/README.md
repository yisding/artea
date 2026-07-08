# policy-sync

Syncs registry policy from the Gitea repo `${ARTEA_NAMESPACE}/registry-policy`
(or explicit `POLICY_REPO`) to the two pull-through caches:

HTTP delivery (K8s) is the only supported deployment; the file-write path
survives for tests and local inspection.

- `npm-rules.yaml` → delivered to the Verdaccio filter plugin two ways:
  - **HTTP (K8s, the supported runtime)**: every successful sync updates an
    in-memory copy served at `GET /policy/npm-rules.yaml`, which the plugin
    polls (`policy_url` mode). `POLICY_FILE_PATH=""` skips the file write
    entirely — there is no `/policy` volume in K8s.
  - **file (test/debug)**: written atomically to `$POLICY_FILE_PATH` (default
    `/policy/npm-rules.yaml`; tmp file + rename, mode 0644). The plugin watches
    the file's mtime; unchanged content is never rewritten, so the mtime only
    moves on real policy changes.
- `upstream-policy.yaml` → delivered to consumers two ways:
  - **HTTP (K8s)**: served at `GET /policy/upstream-policy.yaml`, which the
    Verdaccio filter plugin polls via `upstream_policy_url`.
  - **file (test/debug)**: written atomically to `$UPSTREAM_POLICY_FILE_PATH`
    (default: `/policy/upstream-policy.yaml` next to `npm-rules.yaml`).
  - The same `upstream.min_age` ISO 8601 duration is applied to devpi's
    `root/constrained` index as `min_upstream_age`.
- `pypi-constraints.txt` → optionally written to `$PYPI_POLICY_FILE_PATH` (file
  mode, for debugging; default: next to `npm-rules.yaml` as
  `/policy/pypi-constraints.txt`) and
  applied to the devpi index `root/constrained` via
  devpi's JSON HTTP API (`GET` the index config, `PATCH` it back with the
  `constraints` and `min_upstream_age` keys replaced, Basic auth
  `root:$DEVPI_ROOT_PASSWORD`).
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

Python 3.14, stdlib only — no pip dependencies in the image at all.

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
| `GET /policy/npm-rules.yaml` | The npm policy from the last successful sync, served from memory with a **strong ETag** (quoted SHA-256 of the content) and `If-None-Match`/`304` support. Falls back to reading `$POLICY_FILE_PATH` when memory is empty (file mode after a restart: the file still holds the last synced policy). `404` with a clear JSON body if no policy has ever been synced. **No auth**: the service is cluster-internal and the body is block rules, not secrets — never expose this port or other internal service ports publicly; only the gateway is public. |
| `GET /policy/upstream-policy.yaml` | The shared upstream policy from the last successful sync, with the same ETag behavior as the npm endpoint. Used by the Verdaccio filter in K8s. |
| `POST /osv/querybatch` | Internal request-time OSV malicious-package verdict endpoint for Verdaccio and devpi. Versioned body: `{"ecosystem":"npm\|pypi","name":"...","versions":["..."]}`. Compact package-summary body: `{"ecosystem":"npm\|pypi","name":"...","package_summary":true}` returns only blocked exact MAL versions, or `status:"needs_versions"` when the caller must retry with concrete versions. `blocked_only:true` trims versioned responses to blocking hits. Honors `[osv] malicious_packages`, blocks only `MAL-*` IDs, and lets curated `allow` rules override false positives. |

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
| `POLICY_DIR` | no | `/policy` | Directory the file-mode outputs are written under (file mode; test/debug) |
| `POLICY_FILE_PATH` | no | `$POLICY_DIR/npm-rules.yaml` | Where to write the npm policy file. Set to the empty string for **HTTP-only mode** (K8s: no volume, no file write; the `GET /policy/npm-rules.yaml` endpoint is the only npm-policy output). A custom path gets its parent directory created automatically |
| `UPSTREAM_POLICY_FILE_PATH` | no | sibling `upstream-policy.yaml` next to `$POLICY_FILE_PATH`, or empty when `POLICY_FILE_PATH=""` | Where to write the shared upstream policy file in file mode (test/debug) |
| `PYPI_POLICY_FILE_PATH` | no | sibling `pypi-constraints.txt` next to `$POLICY_FILE_PATH`, or empty when `POLICY_FILE_PATH=""` | Optional file-mode copy of the PyPI constraints file for debugging/inspection |
| `PARSED_POLICY_FILE_PATH` | no | sibling `policy.toml` next to `$POLICY_FILE_PATH`, or empty when `POLICY_FILE_PATH=""` | Last-known-good source policy used by inline OSV decisions after restarts. Empty = in-memory only (OSV fails OPEN until the first post-restart sync). The Helm chart sets this to a path on a small ReadWriteOnce PVC (`policySync.persistence`), so in K8s — including the default HTTP-only deployment where `POLICY_FILE_PATH=""` — OSV blocks fail CLOSED across restarts |
| `POLICY_SYNC_POLL_SECONDS` | no | `300` | Fallback poll interval |
| `OSV_API_URL` | no | `https://api.osv.dev` | OSV-compatible API base used by `/osv/querybatch` |
| `OSV_TIMEOUT_SECONDS` | no | `5` | Per-request OSV API timeout |
| `OSV_POSITIVE_TTL_SECONDS` | no | `3600` | Cache TTL for malicious verdicts |
| `OSV_NEGATIVE_TTL_SECONDS` | no | `900` | Cache TTL for clean verdicts |
| `OSV_BATCH_SIZE` | no | `100` | Number of package versions per fallback OSV `querybatch` call |
| `OSV_MAX_CONCURRENCY` | no | `8` | Maximum concurrent outbound OSV API calls per policy-sync process |
| `OSV_CACHE_FILE_PATH` | no | empty | Optional JSON snapshot path for the bounded OSV verdict cache. Loaded on startup and persisted after successful OSV lookups; entries still obey the positive/negative TTLs above. The Helm chart stores this on the policy-sync state PVC when persistence is enabled |

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
  `npm-rules.yaml`, `upstream-policy.yaml`, and the PyPI constraints file are
  still written — one ecosystem failing never blocks the other.
- **OSV API fails**: `/osv/querybatch` fails open for uncached versions while any
  fresh cached malicious verdicts remain blocking. If `OSV_CACHE_FILE_PATH` is
  set, fresh verdicts can survive a policy-sync restart. This does not weaken the
  compiled curated policy artifacts, which keep their normal fail-closed behavior.
- **Half-written files**: impossible by construction; the rename is atomic and
  the tmp file lives in the same directory/filesystem.

## Development

```sh
cd policy-sync
python3.14 -m pytest          # unit tests, no network, no docker, < 2s
```
