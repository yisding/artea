"""Environment-driven configuration.

Required secrets fail fast at startup (loud misconfiguration beats a silent
no-op service); transient upstream outages are handled at sync time instead.
"""

import os
from dataclasses import dataclass

# Authoritative defaults for the two public-API base URLs, referenced by both the
# dataclass field defaults and the env.get fallbacks below (and osv.py's
# OsvClient.api_url) so the runtime and test-covered copies cannot drift. Kept
# slash-free so the .rstrip("/") on the env fallbacks stays a no-op.
DEFAULT_PYPI_JSON_URL = "https://pypi.org/pypi"
DEFAULT_OSV_API_URL = "https://api.osv.dev"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    gitea_url: str
    sync_token: str
    webhook_secret: str
    policy_repo: str
    policy_file_path: str  # "" = HTTP-only mode (no file write, K8s has no /policy volume)
    upstream_policy_file_path: str  # "" = HTTP-only mode
    pypi_policy_file_path: str
    parsed_policy_file_path: str
    devpi_url: str
    devpi_root_password: str
    poll_interval: float
    # PyPI Simple-API enrichment (PEP 700) reads these; defaults keep existing
    # Config(...) construction sites (tests) working without changes.
    namespace: str = "artea"
    # Retained for env/config parity (PYPI_JSON_URL). The public Simple-API
    # enrichment hot path no longer reads this — it sources per-file metadata from
    # devpi's intra-cluster /+artea/project-meta endpoint, which the devpi age-gate
    # plugin populates from its own (index-configured) pypi_json_url.
    pypi_json_url: str = DEFAULT_PYPI_JSON_URL
    osv_api_url: str = DEFAULT_OSV_API_URL
    osv_timeout_seconds: float = 5.0
    osv_positive_ttl_seconds: float = 3600.0
    osv_negative_ttl_seconds: float = 900.0
    osv_batch_size: int = 100

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        if env is None:
            env = dict(os.environ)

        missing = [k for k in ("POLICY_SYNC_TOKEN", "POLICY_WEBHOOK_SECRET", "DEVPI_ROOT_PASSWORD") if not env.get(k)]
        if missing:
            raise ConfigError(f"missing required environment variables: {', '.join(missing)}")

        try:
            poll_interval = float(env.get("POLICY_SYNC_POLL_SECONDS", "300"))
        except ValueError as e:
            raise ConfigError(f"invalid numeric environment variable: {e}") from e
        try:
            osv_timeout = float(env.get("OSV_TIMEOUT_SECONDS", "5"))
            osv_positive_ttl = float(env.get("OSV_POSITIVE_TTL_SECONDS", "3600"))
            osv_negative_ttl = float(env.get("OSV_NEGATIVE_TTL_SECONDS", "900"))
            osv_batch_size = int(env.get("OSV_BATCH_SIZE", "100"))
        except ValueError as e:
            raise ConfigError(f"invalid numeric environment variable: {e}") from e
        if osv_timeout <= 0 or osv_positive_ttl <= 0 or osv_negative_ttl <= 0 or osv_batch_size <= 0:
            raise ConfigError("OSV timeout, TTLs, and batch size must be positive")

        # POLICY_FILE_PATH unset -> file-mode default under POLICY_DIR (test/local
        # inspection); set to "" -> HTTP-only mode (the /policy endpoint is the only output)
        policy_file_path = env.get("POLICY_FILE_PATH")
        if policy_file_path is None:
            policy_dir = env.get("POLICY_DIR", "/policy").rstrip("/")
            policy_file_path = f"{policy_dir}/npm-rules.yaml"

        # a sibling artifact in the same directory as npm-rules.yaml; "" preserves
        # HTTP-only mode (no policy file path -> no derived file paths either).
        def _sibling(name: str) -> str:
            return os.path.join(os.path.dirname(policy_file_path), name) if policy_file_path else ""

        upstream_policy_file_path = env.get("UPSTREAM_POLICY_FILE_PATH")
        if upstream_policy_file_path is None:
            upstream_policy_file_path = _sibling("upstream-policy.yaml")
        pypi_policy_file_path = env.get("PYPI_POLICY_FILE_PATH")
        if pypi_policy_file_path is None:
            pypi_policy_file_path = _sibling("pypi-constraints.txt")
        parsed_policy_file_path = env.get("PARSED_POLICY_FILE_PATH")
        if parsed_policy_file_path is None:
            parsed_policy_file_path = _sibling("policy.toml")
        namespace = env.get("ARTEA_NAMESPACE", "artea")

        return cls(
            gitea_url=env.get("GITEA_URL", "http://gitea:3000").rstrip("/"),
            sync_token=env["POLICY_SYNC_TOKEN"],
            webhook_secret=env["POLICY_WEBHOOK_SECRET"],
            policy_repo=env.get("POLICY_REPO") or f"{namespace}/registry-policy",
            policy_file_path=policy_file_path,
            upstream_policy_file_path=upstream_policy_file_path,
            pypi_policy_file_path=pypi_policy_file_path,
            parsed_policy_file_path=parsed_policy_file_path,
            devpi_url=env.get("DEVPI_URL", "http://devpi:3141").rstrip("/"),
            devpi_root_password=env["DEVPI_ROOT_PASSWORD"],
            poll_interval=poll_interval,
            namespace=namespace,
            # PyPI JSON API base for PEP 700 upload-time enrichment of public
            # (devpi pull-through) packages; same source the devpi age gate uses.
            pypi_json_url=env.get("PYPI_JSON_URL", DEFAULT_PYPI_JSON_URL).rstrip("/"),
            osv_api_url=env.get("OSV_API_URL", DEFAULT_OSV_API_URL).rstrip("/"),
            osv_timeout_seconds=osv_timeout,
            osv_positive_ttl_seconds=osv_positive_ttl,
            osv_negative_ttl_seconds=osv_negative_ttl,
            osv_batch_size=osv_batch_size,
        )
