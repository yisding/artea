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


def fetch_text(gitea_url: str, api_path: str, authorization: str, timeout: float = 10.0) -> str:
    """GET an arbitrary Gitea path with the CLIENT's Authorization header.

    Unlike fetch_raw (the policy service account token), the PyPI Simple-API
    enrichment forwards the calling user's credential so Gitea re-enforces
    package read permissions. Raises GiteaNotFound on 404 (caller falls through
    to the public mirror), GiteaError otherwise.
    """
    url = f"{gitea_url.rstrip('/')}/{api_path.lstrip('/')}"
    headers = {"Accept": "text/html"}
    if authorization:
        headers["Authorization"] = authorization
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise GiteaNotFound(f"{api_path} not found (HTTP 404)") from e
        raise GiteaError(f"GET {url} failed: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GiteaError(f"GET {url} failed: {e}") from e


def fetch_json(gitea_url: str, api_path: str, authorization: str, timeout: float = 10.0):
    """GET an arbitrary Gitea path as JSON with the CLIENT's Authorization."""
    import json as _json

    url = f"{gitea_url.rstrip('/')}/{api_path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise GiteaNotFound(f"{api_path} not found (HTTP 404)") from e
        raise GiteaError(f"GET {url} failed: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise GiteaError(f"GET {url} failed: {e}") from e
