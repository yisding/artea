"""One policy sync: fetch policy files from Gitea and apply them.

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

from .compiler import compile_policy
from .config import Config
from .devpi import CONSTRAINED_INDEX, DevpiError, apply_constraints
from .files import write_atomic
from .gitea import GiteaError, GiteaNotFound, fetch_raw
from .policy_model import PolicyError, parse_policy
from .store import PolicyStore

log = logging.getLogger(__name__)

UNIFIED_POLICY_FILE = "policy.toml"
NPM_RULES_FILE = "npm-rules.yaml"
PYPI_CONSTRAINTS_FILE = "pypi-constraints.txt"
UPSTREAM_POLICY_FILE = "upstream-policy.yaml"
DEFAULT_MIN_UPSTREAM_AGE = "P0D"


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
        self.min_upstream_age = self._initial_min_upstream_age()

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
        """Returns file bytes, or None if the file should be skipped (404)."""
        try:
            return fetch_raw(self.cfg.gitea_url, self.cfg.policy_repo, path, self.cfg.sync_token)
        except GiteaNotFound:
            log.warning("%s not found in %s; skipping (existing policy untouched)", path, self.cfg.policy_repo)
            return None

    def _extract_min_upstream_age(self, data: bytes) -> str:
        in_upstream = False
        for raw_line in data.decode("utf-8", errors="replace").splitlines():
            line = raw_line.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip(" "))
            stripped = line.strip()
            if indent == 0:
                in_upstream = stripped == "upstream:"
                continue
            if in_upstream and indent > 0:
                key, sep, value = stripped.partition(":")
                if sep and key in {"min_age", "minimum_age", "min_upstream_age"}:
                    return value.strip().strip("\"'")
        return DEFAULT_MIN_UPSTREAM_AGE

    def _initial_min_upstream_age(self) -> str:
        if self.upstream_store is not None:
            got = self.upstream_store.get()
            if got is not None:
                return self._extract_min_upstream_age(got[0])
        dest = self.upstream_dest
        if dest is not None:
            try:
                return self._extract_min_upstream_age(dest.read_bytes())
            except OSError:
                pass
        return DEFAULT_MIN_UPSTREAM_AGE

    def _emit_upstream(self, data: bytes) -> None:
        """Deliver upstream-policy.yaml bytes to the store and/or the shared file."""
        if self.upstream_store is not None:
            self.upstream_store.set(data)
        dest = self.upstream_dest
        if dest is None:
            log.debug("UPSTREAM_POLICY_FILE_PATH empty; HTTP-only mode, no upstream policy file written")
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        if write_atomic(dest, data):
            log.info("wrote %s (%d bytes)", dest, len(data))
        else:
            log.debug("%s unchanged", dest)

    def _sync_upstream(self) -> None:
        data = self._fetch(UPSTREAM_POLICY_FILE)
        if data is None:
            return
        self.min_upstream_age = self._extract_min_upstream_age(data)
        self._emit_upstream(data)

    def _emit_npm(self, data: bytes) -> None:
        """Deliver npm policy bytes to the store and/or the shared volume file."""
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

    def _emit_pypi(self, constraints_text: str | None) -> None:
        """Optionally write the debug file and apply constraints to devpi.

        constraints_text is None only in legacy mode when the legacy file is
        absent (preserve devpi's existing constraints, sync only min_age).
        """
        dest = self.pypi_dest
        if constraints_text is not None and dest is not None:
            data = constraints_text.encode("utf-8")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if write_atomic(dest, data):
                log.info("wrote %s (%d bytes)", dest, len(data))
            else:
                log.debug("%s unchanged", dest)
        # apply_constraints skips the PATCH when devpi already holds these
        # constraints — the live index config, not a local hash, is the
        # idempotency source of truth, so a wiped+recreated devpi (fail-closed
        # '*' seed) is healed by the next poll even with an unchanged policy
        if apply_constraints(self.cfg, constraints_text, self.min_upstream_age):
            if constraints_text is None:
                log.info("applied min_upstream_age=%s to %s", self.min_upstream_age, CONSTRAINED_INDEX)
            else:
                log.info("applied %d bytes of constraints to %s", len(constraints_text), CONSTRAINED_INDEX)
        else:
            log.debug("constraints unchanged; devpi untouched")

    def _sync_npm(self) -> None:
        data = self._fetch(NPM_RULES_FILE)
        if data is None:
            return
        self._emit_npm(data)

    def _sync_pypi(self) -> None:
        data = self._fetch(PYPI_CONSTRAINTS_FILE)
        constraints_text = data.decode("utf-8", errors="replace") if data is not None else None
        self._emit_pypi(constraints_text)

    def _sync_unified(self) -> bool:
        """Try the unified policy.toml compiler path.

        Returns True if the unified file was present and fully handled (npm, pypi,
        and min_age all delivered from policy.toml). Returns False if the unified
        file is ABSENT (404) so the caller falls back to the legacy files. Raises
        PolicyError for a structurally broken policy so the whole sync fails and
        the previously applied policy stays in effect (last-known-good).
        """
        data = self._fetch(UNIFIED_POLICY_FILE)
        if data is None:
            return False  # absent -> legacy fallback; not an error

        # Validate + compile fully before writing anything. A PolicyError here
        # propagates and fails the sync; nothing is written, so enforcement keeps
        # serving the previously applied (last-known-good) policy.
        policy = parse_policy(data)
        artifacts = compile_policy(policy)

        self.min_upstream_age = artifacts.min_age
        # The Verdaccio filter takes min_age solely from upstream-policy.yaml via
        # the CompositePolicyLoader; deliver that artifact exactly like the legacy
        # _sync_upstream so the unified path feeds the npm quarantine gate (and a
        # fresh unified-only deployment does not fail closed for lack of the file).
        self._emit_upstream(artifacts.upstream_yaml.encode("utf-8"))
        self._emit_npm(artifacts.npm_yaml.encode("utf-8"))
        # unified mode is authoritative: emit exactly what compiled (including an
        # empty string, which clears the constraints).
        self._emit_pypi(artifacts.pypi_constraints)
        return True

    def sync_once(self) -> bool:
        """Run one sync. Returns True if nothing retryable failed."""
        try:
            if self._sync_unified():
                return True  # policy.toml owned this sync end to end
        except (GiteaError, DevpiError, OSError) as e:
            log.error("unified policy sync failed: %s", e)
            return False
        except PolicyError as e:
            log.error("unified policy is invalid; keeping last-known-good policy: %s", e)
            return False

        # policy.toml absent -> legacy three-file path, unchanged.
        ok = True
        try:
            self._sync_upstream()
        except (GiteaError, OSError) as e:
            log.error("upstream policy sync failed: %s", e)
            ok = False
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
