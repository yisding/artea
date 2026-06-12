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
    policy_file_path: str  # "" = HTTP-only mode (no file write, K8s has no /policy volume)
    upstream_policy_file_path: str  # "" = HTTP-only mode
    pypi_policy_file_path: str
    devpi_url: str
    devpi_root_password: str
    poll_interval: float

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

        # POLICY_FILE_PATH unset -> compose default under POLICY_DIR;
        # set to "" -> HTTP-only mode (the /policy endpoint is the only output)
        policy_file_path = env.get("POLICY_FILE_PATH")
        if policy_file_path is None:
            policy_dir = env.get("POLICY_DIR", "/policy").rstrip("/")
            policy_file_path = f"{policy_dir}/npm-rules.yaml"
        upstream_policy_file_path = env.get("UPSTREAM_POLICY_FILE_PATH")
        if upstream_policy_file_path is None:
            upstream_policy_file_path = str(os.path.join(os.path.dirname(policy_file_path), "upstream-policy.yaml")) if policy_file_path else ""
        pypi_policy_file_path = env.get("PYPI_POLICY_FILE_PATH")
        if pypi_policy_file_path is None:
            pypi_policy_file_path = str(os.path.join(os.path.dirname(policy_file_path), "pypi-constraints.txt")) if policy_file_path else ""
        namespace = env.get("ARTEA_NAMESPACE", "artea")

        return cls(
            gitea_url=env.get("GITEA_URL", "http://gitea:3000").rstrip("/"),
            sync_token=env["POLICY_SYNC_TOKEN"],
            webhook_secret=env["POLICY_WEBHOOK_SECRET"],
            policy_repo=env.get("POLICY_REPO") or f"{namespace}/registry-policy",
            policy_file_path=policy_file_path,
            upstream_policy_file_path=upstream_policy_file_path,
            pypi_policy_file_path=pypi_policy_file_path,
            devpi_url=env.get("DEVPI_URL", "http://devpi:3141").rstrip("/"),
            devpi_root_password=env["DEVPI_ROOT_PASSWORD"],
            poll_interval=poll_interval,
        )
