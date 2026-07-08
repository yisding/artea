#!/usr/bin/env python3
"""Measure Artea install latency against public PyPI and npm.

The script intentionally benchmarks client-observed install time with fresh
client caches. Server-side registry caches are not flushed; run an uncommon
package once for a first-fetch datapoint, then repeat the same command to see the
warm-cache steady state.
"""

from __future__ import annotations

import argparse
import base64
import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_PYPI_PACKAGES = ("requests==2.32.4",)
DEFAULT_NPM_PACKAGES = ("react@18.2.0",)
SENSITIVE_VALUES: set[str] = set()


@dataclass(frozen=True)
class Measurement:
    ecosystem: str
    registry: str
    run: int
    seconds: float


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        if not key:
            continue
        try:
            value = shlex.split(raw, comments=False, posix=True)[0]
        except (IndexError, ValueError):
            value = raw.strip().strip("'\"")
        out[key] = value
    return out


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not (key.startswith("PIP_") or key.startswith("npm_config_") or key.startswith("NPM_CONFIG_"))
    }
    env.update(
        {
            "PIP_CONFIG_FILE": "/dev/null",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    if extra:
        env.update(extra)
    return env


def run_timed(cmd: list[str], *, env: dict[str, str], cwd: Path, log_path: Path, timeout: int) -> float:
    started = time.perf_counter()
    with log_path.open("wb") as log:
        proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    elapsed = time.perf_counter() - started
    # scrub the persisted log too: --keep-workdir leaves it on disk
    text = redact(log_path.read_text(errors="replace"))
    log_path.write_text(text)
    if proc.returncode != 0:
        tail = "\n".join(text.splitlines()[-40:])
        raise RuntimeError(f"{cmd[0]} exited {proc.returncode}; log tail from {log_path}:\n{tail}")
    return elapsed


def redact(text: str) -> str:
    for value in SENSITIVE_VALUES:
        if value:
            text = text.replace(value, "<redacted>")
    for key in ("DEV1_TOKEN", "ARTEA_ADMIN_TOKEN", "POLICY_SYNC_TOKEN", "DEV1_PASSWORD", "ARTEA_ADMIN_PASSWORD"):
        value = os.environ.get(key)
        if value:
            text = text.replace(value, "<redacted>")
    return text


def write_netrc(home: Path, base_url: str, user: str, token: str) -> None:
    host = urlsplit(base_url).hostname or "localhost"
    path = home / ".netrc"
    path.write_text(f"machine {host}\nlogin {user}\npassword {token}\n")
    path.chmod(0o600)


def write_npmrc(path: Path, registry: str, user: str | None = None, token: str | None = None) -> None:
    lines = [
        f"registry={registry.rstrip('/')}/",
        "audit=false",
        "fund=false",
        "update-notifier=false",
    ]
    if user and token:
        split = urlsplit(registry)
        hostport = split.netloc
        auth = base64.b64encode(f"{user}:{token}".encode()).decode()
        lines.extend(
            [
                f"//{hostport}/:_auth={auth}",
                f"//{hostport}/npm/:_auth={auth}",
            ]
        )
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def create_venv(path: Path) -> Path:
    subprocess.run([sys.executable, "-m", "venv", str(path)], check=True, stdout=subprocess.DEVNULL)
    return path / "bin" / "python"


def measure_pypi(
    root: Path,
    run_no: int,
    registry_name: str,
    index_url: str,
    packages: tuple[str, ...],
    home: Path,
    timeout: int,
) -> Measurement:
    work = root / f"pypi-{registry_name}-{run_no}"
    work.mkdir()
    python = create_venv(work / "venv")
    target = work / "target"
    cache = work / "cache"
    log_path = work / "pip.log"
    env = clean_env({"HOME": str(home), "PIP_CACHE_DIR": str(cache)})
    cmd = [
        str(python),
        "-m",
        "pip",
        "install",
        "-q",
        "--no-cache-dir",
        "--no-input",
        "--index-url",
        index_url.rstrip("/") + "/",
        "--target",
        str(target),
        *packages,
    ]
    seconds = run_timed(cmd, env=env, cwd=work, log_path=log_path, timeout=timeout)
    return Measurement("pypi", registry_name, run_no, seconds)


def measure_npm(
    root: Path,
    run_no: int,
    registry_name: str,
    registry_url: str,
    packages: tuple[str, ...],
    user: str | None,
    token: str | None,
    timeout: int,
) -> Measurement:
    work = root / f"npm-{registry_name}-{run_no}"
    work.mkdir()
    npmrc = work / ".npmrc"
    cache = work / "cache"
    write_npmrc(npmrc, registry_url, user, token)
    package_json = {
        "name": "artea-perf-benchmark",
        "version": "0.0.0",
        "private": True,
        "dependencies": {pkg.rsplit("@", 1)[0]: pkg.rsplit("@", 1)[1] for pkg in packages},
    }
    import json

    (work / "package.json").write_text(json.dumps(package_json, indent=2) + "\n")
    env = clean_env(
        {
            "npm_config_userconfig": str(npmrc),
            "npm_config_cache": str(cache),
            "npm_config_loglevel": "error",
        }
    )
    cmd = ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"]
    seconds = run_timed(cmd, env=env, cwd=work, log_path=work / "npm.log", timeout=timeout)
    return Measurement("npm", registry_name, run_no, seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL"))
    parser.add_argument("--credentials-file", default=os.environ.get("CREDENTIALS_FILE", "e2e/tmp/credentials.env"))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--pypi-package", action="append", dest="pypi_packages")
    parser.add_argument("--npm-package", action="append", dest="npm_packages")
    parser.add_argument("--skip-pypi", action="store_true")
    parser.add_argument("--skip-npm", action="store_true")
    parser.add_argument("--keep-workdir", action="store_true")
    return parser.parse_args()


def ratio_line(artea: Measurement, upstream: Measurement) -> str:
    ratio = artea.seconds / upstream.seconds if upstream.seconds else float("inf")
    status = "OK" if ratio <= 3 else "SLOW"
    return (
        f"{artea.ecosystem:4} run {artea.run}: "
        f"upstream={upstream.seconds:7.2f}s  artea={artea.seconds:7.2f}s  "
        f"ratio={ratio:5.2f}x  {status}"
    )


def main() -> int:
    args = parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat must be >= 1")

    credentials_file = Path(args.credentials_file)
    creds = parse_env_file(credentials_file) if credentials_file.exists() else {}
    base_url = (args.base_url or creds.get("GATEWAY_URL") or "http://localhost:8080").rstrip("/")
    user = creds.get("DEV1_USER")
    token = creds.get("DEV1_TOKEN")
    SENSITIVE_VALUES.update(v for v in creds.values() if v)
    if (not args.skip_pypi or not args.skip_npm) and (not user or not token):
        raise SystemExit(f"{credentials_file} must define DEV1_USER and DEV1_TOKEN")

    pypi_packages = tuple(args.pypi_packages or DEFAULT_PYPI_PACKAGES)
    npm_packages = tuple(args.npm_packages or DEFAULT_NPM_PACKAGES)

    root_obj = tempfile.TemporaryDirectory(prefix="artea-perf-")
    root = Path(root_obj.name)
    if args.keep_workdir:
        print(f"workdir: {root}")
    home = root / "home"
    home.mkdir()
    write_netrc(home, base_url, user or "", token or "")

    measurements: list[Measurement] = []
    try:
        for run_no in range(1, args.repeat + 1):
            if not args.skip_pypi:
                upstream = measure_pypi(
                    root,
                    run_no,
                    "upstream",
                    "https://pypi.org/simple/",
                    pypi_packages,
                    home,
                    args.timeout,
                )
                artea = measure_pypi(
                    root,
                    run_no,
                    "artea",
                    f"{base_url}/pypi/simple/",
                    pypi_packages,
                    home,
                    args.timeout,
                )
                measurements.extend([upstream, artea])
                print(ratio_line(artea, upstream), flush=True)
            if not args.skip_npm:
                upstream = measure_npm(
                    root,
                    run_no,
                    "upstream",
                    "https://registry.npmjs.org/",
                    npm_packages,
                    None,
                    None,
                    args.timeout,
                )
                artea = measure_npm(
                    root,
                    run_no,
                    "artea",
                    f"{base_url}/npm/",
                    npm_packages,
                    user,
                    token,
                    args.timeout,
                )
                measurements.extend([upstream, artea])
                print(ratio_line(artea, upstream), flush=True)
    finally:
        if not args.keep_workdir:
            root_obj.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
