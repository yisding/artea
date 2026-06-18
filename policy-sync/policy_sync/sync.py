"""One policy sync: fetch policy.toml from Gitea, compile it, and apply.

Failure semantics:
- an absent or structurally invalid policy.toml fails the sync (the caller
  retries with backoff) and keeps the previously applied policy in effect —
  enforcement is never torn down by a missing or broken policy;
- network/5xx errors and devpi failures mark the sync failed so the caller
  retries with backoff;
- nothing here raises out of sync_with_retry(); the service must never
  crash-loop because Gitea or devpi is down.
"""

import logging
import time
from pathlib import Path

from .compiler import compile_policy
from .config import Config
from .devpi import CONSTRAINED_INDEX, DevpiError, apply_constraints
from .files import write_atomic
from .gitea import GiteaError, GiteaNotFound, fetch_raw
from .policy_model import PolicyError, parse_policy
from .store import PolicyStore

log = logging.getLogger(__name__)

POLICY_FILE = "policy.toml"


class Syncer:
    def __init__(
        self,
        cfg: Config,
        sleep=time.sleep,
        store: PolicyStore | None = None,
        upstream_store: PolicyStore | None = None,
    ):
        self.cfg = cfg
        self.sleep = sleep
        self.store = store
        self.upstream_store = upstream_store

    @property
    def npm_dest(self) -> Path | None:
        return Path(self.cfg.policy_file_path) if self.cfg.policy_file_path else None

    @property
    def pypi_dest(self) -> Path | None:
        return Path(self.cfg.pypi_policy_file_path) if self.cfg.pypi_policy_file_path else None

    @property
    def upstream_dest(self) -> Path | None:
        return Path(self.cfg.upstream_policy_file_path) if self.cfg.upstream_policy_file_path else None

    def _fetch(self, path: str) -> bytes | None:
        """Returns file bytes, or None if the file is absent (404)."""
        try:
            return fetch_raw(self.cfg.gitea_url, self.cfg.policy_repo, path, self.cfg.sync_token)
        except GiteaNotFound:
            return None

    def _write(self, dest: Path | None, data: bytes) -> None:
        """Atomically write data to dest (a no-op in HTTP-only mode, dest=None)."""
        if dest is None:
            return
        dest.parent.mkdir(parents=True, exist_ok=True)  # private tmp dirs may not exist yet
        if write_atomic(dest, data):
            log.info("wrote %s (%d bytes)", dest, len(data))
        else:
            log.debug("%s unchanged", dest)

    def _emit_upstream(self, data: bytes) -> None:
        if self.upstream_store is not None:
            self.upstream_store.set(data)
        self._write(self.upstream_dest, data)

    def _emit_npm(self, data: bytes) -> None:
        if self.store is not None:
            self.store.set(data)  # the /policy endpoint serves the new policy immediately
        self._write(self.npm_dest, data)

    def _emit_pypi(self, constraints_text: str, min_age: str) -> None:
        self._write(self.pypi_dest, constraints_text.encode("utf-8"))
        # apply_constraints skips the PATCH when devpi already holds these
        # constraints — the live index config, not a local hash, is the
        # idempotency source of truth, so a wiped+recreated devpi (fail-closed
        # '*' seed) is healed by the next poll even with an unchanged policy.
        if apply_constraints(self.cfg, constraints_text, min_age):
            log.info("applied %d bytes of constraints to %s", len(constraints_text), CONSTRAINED_INDEX)
        else:
            log.debug("constraints unchanged; devpi untouched")

    def sync_once(self) -> bool:
        """Run one sync. Returns True on success; False on any failure, leaving
        the previously applied policy in effect (last-known-good)."""
        try:
            data = self._fetch(POLICY_FILE)
            if data is None:
                log.error("%s not found in %s; keeping last-known-good policy", POLICY_FILE, self.cfg.policy_repo)
                return False
            # Validate + compile fully before writing anything. A PolicyError
            # here fails the sync; nothing is written, so enforcement keeps
            # serving the previously applied (last-known-good) policy.
            policy = parse_policy(data)
            artifacts = compile_policy(policy)
            # The Verdaccio CompositePolicyLoader takes min_age solely from
            # upstream-policy.yaml, so emit that first — it feeds the npm
            # quarantine gate (a fresh deployment must not fail closed for lack
            # of the file). npm is written before the devpi PATCH so a devpi
            # outage still publishes the npm policy.
            self._emit_upstream(artifacts.upstream_yaml.encode("utf-8"))
            self._emit_npm(artifacts.npm_yaml.encode("utf-8"))
            self._emit_pypi(artifacts.pypi_constraints, artifacts.min_age)
            return True
        except PolicyError as e:
            log.error("policy is invalid; keeping last-known-good policy: %s", e)
            return False
        except (GiteaError, DevpiError, OSError) as e:
            log.error("policy sync failed: %s", e)
            return False

    def sync_with_retry(self, attempts: int = 5, base_delay: float = 2.0, max_delay: float = 60.0) -> bool:
        """Retry sync_once with exponential backoff. Never raises."""
        delay = base_delay
        for attempt in range(1, attempts + 1):
            try:
                if self.sync_once():
                    return True
            except Exception:
                # belt and braces: sync_once should catch everything expected
                log.exception("unexpected error during sync")
            if attempt < attempts:
                log.warning("sync attempt %d/%d failed; retrying in %.0fs", attempt, attempts, delay)
                self.sleep(delay)
                delay = min(delay * 2, max_delay)
        log.error("sync failed after %d attempts; will retry on next webhook or poll", attempts)
        return False
