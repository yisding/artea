"""One policy sync: fetch both policy files from Gitea and apply them.

Failure semantics:
- a missing file (404) is a warning and is skipped — deleting one policy file
  from the repo must not break the other policy path;
- network/5xx errors and devpi failures mark the sync failed so the caller
  retries with backoff;
- nothing here raises out of sync_with_retry(); the service must never
  crash-loop because Gitea or devpi is down.
"""

import hashlib
import logging
import time
from pathlib import Path

from .config import Config
from .devpi import DevpiError, apply_constraints
from .files import write_atomic
from .gitea import GiteaError, GiteaNotFound, fetch_raw

log = logging.getLogger(__name__)

NPM_RULES_FILE = "npm-rules.yaml"
PYPI_CONSTRAINTS_FILE = "pypi-constraints.txt"


class Syncer:
    def __init__(self, cfg: Config, sleep=time.sleep):
        self.cfg = cfg
        self.sleep = sleep
        # hash of last successfully applied constraints, to skip devpi churn
        # on every poll when nothing changed
        self._applied_constraints: str | None = None

    @property
    def npm_dest(self) -> Path:
        return Path(self.cfg.policy_dir) / NPM_RULES_FILE

    def _fetch(self, path: str) -> bytes | None:
        """Returns file bytes, or None if the file should be skipped (404)."""
        try:
            return fetch_raw(self.cfg.gitea_url, self.cfg.policy_repo, path, self.cfg.sync_token)
        except GiteaNotFound:
            log.warning("%s not found in %s; skipping (existing policy untouched)", path, self.cfg.policy_repo)
            return None

    def _sync_npm(self) -> None:
        data = self._fetch(NPM_RULES_FILE)
        if data is None:
            return
        if write_atomic(self.npm_dest, data):
            log.info("wrote %s (%d bytes)", self.npm_dest, len(data))
        else:
            log.debug("%s unchanged", self.npm_dest)

    def _sync_pypi(self) -> None:
        data = self._fetch(PYPI_CONSTRAINTS_FILE)
        if data is None:
            return
        digest = hashlib.sha256(data).hexdigest()
        if digest == self._applied_constraints:
            log.debug("constraints unchanged; not touching devpi")
            return
        apply_constraints(self.cfg, data.decode("utf-8", errors="replace"))
        self._applied_constraints = digest
        log.info("applied %d bytes of constraints to %s", len(data), self.cfg.devpi_index)

    def sync_once(self) -> bool:
        """Run one sync. Returns True if nothing retryable failed."""
        ok = True
        try:
            self._sync_npm()
        except (GiteaError, OSError) as e:
            log.error("npm policy sync failed: %s", e)
            ok = False
        try:
            self._sync_pypi()
        except (GiteaError, DevpiError) as e:
            log.error("pypi constraints sync failed: %s", e)
            ok = False
        return ok

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
