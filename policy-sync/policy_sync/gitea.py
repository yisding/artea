"""Fetch raw file contents from a Gitea repository via the v1 API."""

import logging
import urllib.error
import urllib.request
from urllib.parse import quote

log = logging.getLogger(__name__)


class GiteaError(Exception):
    """Retryable fetch failure (network error, 5xx, auth misconfiguration)."""


class GiteaNotFound(GiteaError):
    """File (or repo) does not exist; not retryable, caller should skip."""


def fetch_raw(gitea_url: str, repo: str, path: str, token: str, timeout: float = 10.0) -> bytes:
    url = f"{gitea_url.rstrip('/')}/api/v1/repos/{repo}/raw/{quote(path)}"
    req = urllib.request.Request(url, headers={"Authorization": f"token {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise GiteaNotFound(f"{repo}:{path} not found (HTTP 404)") from e
        raise GiteaError(f"GET {url} failed: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GiteaError(f"GET {url} failed: {e}") from e
