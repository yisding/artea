"""Environment-driven configuration.

Required secrets fail fast at startup (loud misconfiguration beats a silent
no-op service); transient upstream outages are handled at sync time instead.
"""

import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    gitea_url: str
    sync_token: str
    webhook_secret: str
    policy_repo: str
    policy_dir: str
    devpi_url: str
    devpi_root_password: str
    devpi_index: str
    port: int
    poll_interval: float

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        if env is None:
            env = dict(os.environ)

        missing = [k for k in ("POLICY_SYNC_TOKEN", "POLICY_WEBHOOK_SECRET", "DEVPI_ROOT_PASSWORD") if not env.get(k)]
        if missing:
            raise ConfigError(f"missing required environment variables: {', '.join(missing)}")

        try:
            port = int(env.get("POLICY_SYNC_PORT", "8920"))
            poll_interval = float(env.get("POLICY_SYNC_POLL_SECONDS", "300"))
        except ValueError as e:
            raise ConfigError(f"invalid numeric environment variable: {e}") from e

        return cls(
            gitea_url=env.get("GITEA_URL", "http://gitea:3000").rstrip("/"),
            sync_token=env["POLICY_SYNC_TOKEN"],
            webhook_secret=env["POLICY_WEBHOOK_SECRET"],
            policy_repo=env.get("POLICY_REPO", "artea/registry-policy"),
            policy_dir=env.get("POLICY_DIR", "/policy"),
            devpi_url=env.get("DEVPI_URL", "http://devpi:3141").rstrip("/"),
            devpi_root_password=env["DEVPI_ROOT_PASSWORD"],
            devpi_index=env.get("DEVPI_INDEX", "root/constrained"),
            port=port,
            poll_interval=poll_interval,
        )
