"""Idempotency tests for devpi/entrypoint.sh.

Runs the real entrypoint (and the real ensure_index.py) with fake
devpi-init/devpi-server binaries on PATH — no docker, no network, no devpi
install needed. The fake devpi-server actually listens on the chosen port and
emulates the index JSON API, so the readiness probe and the ensure-index HTTP
calls are exercised for real. Every fake call is appended to FAKE_LOG so tests
can assert exactly what ran.

Run: python3 -m pytest devpi/tests/ -q  (stdlib + pytest only)
"""

import os
import socket
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parent.parent / "entrypoint.sh"

FAKE_INIT = """\
#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("devpi-init " + " ".join(args) + "\\n")
serverdir = args[args.index("--serverdir") + 1]
marker = os.path.join(serverdir, ".serverversion")
if os.path.exists(marker):
    sys.exit("fatal: server dir already initialized")  # mirrors real devpi-init
os.makedirs(serverdir, exist_ok=True)
with open(marker, "w") as f:
    f.write("fake")
"""

# listens for real; /root/constrained existence is tracked via a marker file so
# state survives container "restarts" within a test
FAKE_SERVER = """\
#!/usr/bin/env python3
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("devpi-server " + " ".join(args) + "\\n")
port = int(args[args.index("--port") + 1])
marker = os.path.join(os.environ["FAKE_STATE_DIR"], "index_root_constrained")
core_marker = os.path.join(os.environ["FAKE_STATE_DIR"], "pypi_core_metadata")

class Handler(BaseHTTPRequestHandler):
    def _log(self, line):
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(line + "\\n")

    def _reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path.startswith("/root/constrained"):
            self._log("GET /root/constrained")
            if os.path.exists(marker):
                bases = ["wrong/base"] if os.environ.get("FAKE_BAD_BASES") else ["root/pypi"]
                self._reply(200, '{"result": {"type": "constrained", "bases": %s}}' % json.dumps(bases))
            else:
                self._reply(404, '{}')
        elif self.path.startswith("/root/pypi"):
            self._log("GET /root/pypi")
            provides = "true" if os.path.exists(core_marker) else "false"
            self._reply(200, '{"result": {"type": "mirror", "mirror_provides_core_metadata": %s}}' % provides)
        else:  # /+status readiness probe etc.
            self._reply(200, '{}')

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        authed = "authed" if self.headers.get("Authorization") else "anon"
        self._log("PUT %s %s %s" % (self.path, authed, body))
        open(marker, "w").close()
        self._reply(200, '{}')

    def do_PATCH(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        authed = "authed" if self.headers.get("Authorization") else "anon"
        self._log("PATCH %s %s %s" % (self.path, authed, body))
        if self.path.startswith("/root/pypi") and "mirror_provides_core_metadata" in body:
            open(core_marker, "w").close()
        self._reply(200, '{}')

    def log_message(self, *a):
        pass

HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def env(tmp_path):
    bins = tmp_path / "bin"
    bins.mkdir()
    for name, body in [("devpi-init", FAKE_INIT), ("devpi-server", FAKE_SERVER)]:
        p = bins / name
        p.write_text(body)
        p.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    log = tmp_path / "calls.log"
    log.touch()
    e = os.environ.copy()
    e.update({
        "PATH": f"{bins}:{e['PATH']}",
        "FAKE_LOG": str(log),
        "FAKE_STATE_DIR": str(state),
        "DEVPISERVER_SERVERDIR": str(tmp_path / "server"),
        "DEVPI_ROOT_PASSWORD": "s3cret",
        "DEVPI_PORT": str(free_port()),
        "DEVPI_STARTUP_TIMEOUT": "15",
        "DEVPI_ONESHOT": "1",  # exit after init instead of serving forever
    })
    return e


def run_entrypoint(e):
    return subprocess.run(
        ["bash", str(ENTRYPOINT)], env=e,
        capture_output=True, text=True, timeout=60,
    )


def calls(e):
    return Path(e["FAKE_LOG"]).read_text().splitlines()


def test_first_boot_inits_and_creates_index(env):
    res = run_entrypoint(env)
    assert res.returncode == 0, res.stderr
    lines = calls(env)
    assert sum(c.startswith("devpi-init ") for c in lines) == 1
    creates = [c for c in lines if c.startswith("PUT /root/constrained")]
    assert len(creates) == 1
    assert "authed" in creates[0]  # sent root credentials
    assert '"type": "constrained"' in creates[0]
    assert '"bases": "root/pypi"' in creates[0]
    # fail-closed seed (S15): a fresh index blocks everything until policy-sync
    assert '"constraints": ["*"]' in creates[0]
    assert '"min_upstream_age": "P0D"' in creates[0]
    server = [c for c in lines if c.startswith("devpi-server ")]
    assert len(server) == 1
    assert "--host 0.0.0.0" in server[0]
    assert "--outside-url http://localhost:8080" in server[0]
    assert "--absolute-urls" in server[0]  # file hrefs must be gateway-absolute
    assert "--enable-core-metadata" in server[0]  # PEP 658/714 server-wide switch
    assert (Path(env["DEVPISERVER_SERVERDIR"]) / ".serverversion").exists()
    # PEP 658: the per-mirror option is turned on for root/pypi (the server flag
    # alone does nothing without it).
    patches = [c for c in lines if c.startswith("PATCH /root/pypi")]
    assert len(patches) == 1
    assert "authed" in patches[0]  # sent root credentials
    assert "mirror_provides_core_metadata" in patches[0]


def test_root_pypi_core_metadata_enabled_idempotently(env):
    # First boot turns on mirror_provides_core_metadata; a second boot sees it
    # already set (GET /root/pypi) and must NOT PATCH again.
    assert run_entrypoint(env).returncode == 0
    seen = len(calls(env))
    assert run_entrypoint(env).returncode == 0
    new = calls(env)[seen:]
    assert any(c == "GET /root/pypi" for c in new), "existence was re-checked"
    assert not any(c.startswith("PATCH /root/pypi") for c in new), "already enabled, no re-PATCH"


def test_second_boot_is_idempotent(env):
    assert run_entrypoint(env).returncode == 0
    seen = len(calls(env))
    res = run_entrypoint(env)
    assert res.returncode == 0, res.stderr
    new = calls(env)[seen:]
    assert not any(c.startswith("devpi-init ") for c in new)
    # no PUT at all: an existing index's constraints are never overwritten
    assert not any(c.startswith("PUT ") for c in new)
    assert any(c == "GET /root/constrained" for c in new)  # existence was checked


def test_index_recreated_if_missing(env):
    # server dir initialized but index gone (partial wipe) -> created again
    assert run_entrypoint(env).returncode == 0
    (Path(env["FAKE_STATE_DIR"]) / "index_root_constrained").unlink()
    res = run_entrypoint(env)
    assert res.returncode == 0, res.stderr
    creates = [c for c in calls(env) if c.startswith("PUT /root/constrained")]
    assert len(creates) == 2


def test_existing_index_requires_root_pypi_base(env):
    assert run_entrypoint(env).returncode == 0
    env["FAKE_BAD_BASES"] = "1"
    res = run_entrypoint(env)
    assert res.returncode != 0
    assert "expected root/pypi" in res.stderr


def test_requires_root_password(env):
    env.pop("DEVPI_ROOT_PASSWORD")
    res = run_entrypoint(env)
    assert res.returncode != 0
    assert "DEVPI_ROOT_PASSWORD" in res.stderr
    assert calls(env) == []  # failed before touching anything
    assert not Path(env["DEVPISERVER_SERVERDIR"]).exists()
