"""Apply PyPI constraints to the devpi `root/constrained` index via its JSON API.

devpi-client cannot be used here: the server runs with --outside-url, so the
client's /+api discovery rewrites its target URL to the gateway origin, which
is not devpi (and not reachable) from inside the compose network — see
devpi/README.md. Raw HTTP against http://devpi:3141 is unaffected.

devpi-constrained stores constraints as an index property; replacing the whole
property is idempotent, and the raw pypi-constraints.txt text can be pushed
as-is (blank lines and # comments are part of the format).
"""

import base64
import json
import logging
import urllib.error
import urllib.request

from .config import Config

log = logging.getLogger(__name__)


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


def apply_constraints(cfg: Config, constraints_text: str) -> None:
    url = f"{cfg.devpi_url}/{cfg.devpi_index}"
    config = _request(urllib.request.Request(url, headers={"Accept": "application/json"})).get("result")
    if not isinstance(config, dict):
        raise DevpiError(f"GET {url}: response has no index config in .result")

    config["constraints"] = constraints_text
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
