"""One policy sync: fetch both policy files from Gitea and apply them.

Failure semantics:
- a missing file (404) is a warning and is skipped — deleting one policy file
  from the repo must not break the other policy path;
- network/5xx errors and devpi failures mark the sync failed so the caller
  retries with backoff;
- nothing here raises out of sync_with_retry(); the service must never
  crash-loop because Gitea or devpi is down.
"""

import logging
import time
from pathlib import Path

from .config import Config
from .devpi import CONSTRAINED_INDEX, DevpiError, apply_constraints
from .files import write_atomic
from .gitea import GiteaError, GiteaNotFound, fetch_raw
from .store import PolicyStore

log = logging.getLogger(__name__)

NPM_RULES_FILE = "npm-rules.yaml"
PYPI_CONSTRAINTS_FILE = "pypi-constraints.txt"


class Syncer:
    def __init__(self, cfg: Config, sleep=time.sleep, store: PolicyStore | None = None):
        self.cfg = cfg
        self.sleep = sleep
        self.store = store

    @property
    def npm_dest(self) -> Path | None:
        return Path(self.cfg.policy_file_path) if self.cfg.policy_file_path else None

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
        if self.store is not None:
            self.store.set(data)  # the /policy endpoint serves the new policy immediately
        dest = self.npm_dest
        if dest is None:
            log.debug("POLICY_FILE_PATH empty; HTTP-only mode, no file written")
            return
        dest.parent.mkdir(parents=True, exist_ok=True)  # private tmp dirs may not exist yet
        if write_atomic(dest, data):
            log.info("wrote %s (%d bytes)", dest, len(data))
        else:
            log.debug("%s unchanged", dest)

    def _sync_pypi(self) -> None:
        data = self._fetch(PYPI_CONSTRAINTS_FILE)
        if data is None:
            return
        # apply_constraints skips the PATCH when devpi already holds these
        # constraints — the live index config, not a local hash, is the
        # idempotency source of truth, so a wiped+recreated devpi (fail-closed
        # '*' seed) is healed by the next poll even with an unchanged policy
        if apply_constraints(self.cfg, data.decode("utf-8", errors="replace")):
            log.info("applied %d bytes of constraints to %s", len(data), CONSTRAINED_INDEX)
        else:
            log.debug("constraints unchanged; devpi untouched")

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
