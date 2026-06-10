"""Atomic policy-file writes.

The Verdaccio filter plugin watches the mtime of /policy/npm-rules.yaml, so:
- writes must be atomic (tmp file in the same directory + rename) so the
  plugin never reads a half-written file;
- unchanged content must not be rewritten, so the mtime only moves when the
  policy actually changed.
"""

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


def write_atomic(dest: Path, content: bytes) -> bool:
    """Write content to dest atomically. Returns True if the file changed."""
    dest = Path(dest)
    if dest.exists() and dest.read_bytes() == content:
        return False

    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates 0600; other containers (Verdaccio) must read the file
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True
