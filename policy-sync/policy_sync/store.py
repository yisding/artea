"""In-memory copy of the last successfully synced npm policy.

Backs `GET /policy/npm-rules.yaml` (K8s mode: the Verdaccio filter plugin polls
this instead of reading the shared volume). The ETag is a strong content hash,
so it is stable across restarts and identical for byte-identical policies.
set() swaps content and ETag under one lock, so readers never see a mismatch.

An optional fallback file path (the compose-mode /policy file) covers restarts:
the volume still holds the last synced policy even before the first sync of the
new process succeeds.
"""

import hashlib
import threading
from pathlib import Path

from .policy_model import Policy


def etag_for(content: bytes) -> str:
    return f'"{hashlib.sha256(content).hexdigest()}"'


class PolicyStore:
    def __init__(self, fallback_path: str = ""):
        self._lock = threading.Lock()
        self._content: bytes | None = None
        self._etag: str | None = None
        self.fallback_path = fallback_path

    def set(self, content: bytes) -> None:
        etag = etag_for(content)  # hash outside the lock
        with self._lock:
            self._content = content
            self._etag = etag

    def get(self) -> tuple[bytes, str] | None:
        """Returns (content, etag), or None if no policy has ever been synced."""
        with self._lock:
            if self._content is not None:
                return self._content, self._etag  # type: ignore[return-value]
        if self.fallback_path:
            try:
                content = Path(self.fallback_path).read_bytes()
            except OSError:
                return None
            return content, etag_for(content)
        return None


class ParsedPolicyStore:
    """In-memory last-known-good parsed policy for request-time decisions.

    The emitted engine artifacts remain the durable enforcement source. This store
    only backs inline decisions that need the unified policy's metadata, such as
    whether OSV-generated denies are enabled and whether a curated allow overrides
    an OSV malicious-package hit.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._policy: Policy | None = None

    def set(self, policy: Policy) -> None:
        with self._lock:
            self._policy = policy

    def get(self) -> Policy | None:
        with self._lock:
            return self._policy
