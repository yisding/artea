"""Apply PyPI constraints to the devpi `root/constrained` index via its JSON API.

devpi-client cannot be used here: the server runs with --outside-url, so the
client's /+api discovery rewrites its target URL to the gateway origin, which
is not devpi (and not reachable) from inside the cluster network — see
devpi/README.md. Raw HTTP against http://devpi:3141 is unaffected.

The Artea devpi policy plugin stores constraints and minimum upstream age as
index properties. Replacing those properties is idempotent, and the raw
pypi-constraints.txt text can be pushed as-is (blank lines and # comments are
part of the format).

The index config fetched from devpi — not any local state — decides whether a
PATCH is needed: a wiped devpi-data volume comes back with the entrypoint's
fail-closed '*' seed (see devpi/ensure_index.py) and must be healed by the
next sync even though the policy file itself did not change.
"""

import base64
import json
import logging
import urllib.error
import urllib.request

from .config import Config

log = logging.getLogger(__name__)
CONSTRAINED_INDEX = "root/constrained"


class DevpiError(Exception):
    pass


def _request(req: urllib.request.Request, timeout: float = 30.0) -> dict:
    # error text is built from URLs and status codes only — the root password
    # lives in the Authorization header and never reaches an exception message
    what = f"{req.get_method()} {req.full_url}"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise DevpiError(f"{what} -> HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise DevpiError(f"{what} failed: {e}") from e


def _effective_lines(value) -> list[str] | None:
    """Constraints reduced to the constrained index's effective form: stripped
    lines minus blanks and #-comment lines (verified against a live server,
    which stores/returns exactly that). Accepts the raw file text (str) or
    the stored index value (list); None for anything else, which never
    compares equal, so callers re-apply."""
    if isinstance(value, str):
        lines = value.splitlines()
    elif isinstance(value, (list, tuple)):
        lines = [str(v) for v in value]
    else:
        return None
    return [s for s in (line.strip() for line in lines) if s and not s.startswith("#")]


def _has_expected_base(value) -> bool:
    return value == "root/pypi" or value == ["root/pypi"]


def apply_constraints(cfg: Config, constraints_text: str, min_upstream_age: str) -> bool:
    """Ensure the index holds constraints_text and min_upstream_age.

    Returns True if devpi was PATCHed.
    """
    url = f"{cfg.devpi_url}/{CONSTRAINED_INDEX}"
    config = _request(urllib.request.Request(url, headers={"Accept": "application/json"})).get("result")
    if not isinstance(config, dict):
        raise DevpiError(f"GET {url}: response has no index config in .result")
    if config.get("type") != "constrained":
        raise DevpiError(f"GET {url}: index type is {config.get('type')!r}, expected 'constrained'")
    if not _has_expected_base(config.get("bases")):
        raise DevpiError(f"GET {url}: index bases are {config.get('bases')!r}, expected root/pypi")

    if (
        _effective_lines(config.get("constraints")) == _effective_lines(constraints_text)
        and config.get("min_upstream_age", "P0D") == min_upstream_age
    ):
        log.debug("%s already holds these constraints; no PATCH", CONSTRAINED_INDEX)
        return False

    config["constraints"] = constraints_text
    config["min_upstream_age"] = min_upstream_age
    auth = base64.b64encode(f"root:{cfg.devpi_root_password}".encode()).decode()
    _request(
        urllib.request.Request(
            url,
            data=json.dumps(config).encode(),
            method="PATCH",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Basic {auth}",
            },
        )
    )
    return True
