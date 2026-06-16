"""Emit-contract tests: prove the compiler's artifacts are accepted by the REAL
engines, not just by the compiler's own conservative validators.

The audit's root cause (A1/A3) was that the compiler validated its emitted output
against its OWN regex, not the engine, so it could emit an artifact the engine
REJECTS and fail-close the whole ecosystem. These tests close that gap:

- npm side: feed representative compiled npm YAML through the real Verdaccio
  filter ``compilePolicy`` (the same function the plugin runs). It is built from
  ``src/policy.ts`` in a THROWAWAY dir (the plugin's own tsc + node_modules via
  NODE_PATH); no plugin source / package.json / lock is touched. SKIPPED when node
  or the plugin tree is unavailable.
- pypi side: feed compiled constraints through devpi's real ``parse_constraints``
  if importable (it needs pkg_resources/packaging); otherwise through the stdlib
  contract reimplementation MockDevpi uses (the default CI path here, since the
  test venv lacks those libs).
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.conftest import parse_constraints_contract
from policy_sync.compiler import compile_policy
from policy_sync.policy_model import parse_policy

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_DIR = _REPO_ROOT / "verdaccio" / "plugins" / "verdaccio-filter-artea"
_PLUGIN_SRC = _PLUGIN_DIR / "src" / "policy.ts"
_PLUGIN_TSC = _PLUGIN_DIR / "node_modules" / ".bin" / "tsc"
_PLUGIN_NODE_MODULES = _PLUGIN_DIR / "node_modules"


def _compile(text: str):
    return compile_policy(parse_policy(text.encode("utf-8")))


# representative policies exercising every npm emission path.
_NPM_POLICIES = [
    # whole-package deny -> bare string
    'schema = 1\n[[rules]]\necosystem = "npm"\nname = "event-stream"\naction = "deny"\n',
    # scope deny
    'schema = 1\n[[rules]]\necosystem = "npm"\nnamespace = "evil-corp"\naction = "deny"\n',
    # range deny incl. trailing wildcard
    'schema = 1\n[[rules]]\necosystem = "npm"\nname = "left-pad"\nversions = "1.2.x"\naction = "deny"\n',
    # exact-allow complement against a whole deny
    (
        'schema = 1\n'
        '[[rules]]\necosystem = "npm"\nname = "p"\naction = "deny"\n'
        '[[rules]]\necosystem = "npm"\nname = "p"\nversions = "1.2.3"\naction = "allow"\n'
    ),
    # reason carrying control chars (A2): must round-trip through js-yaml exactly
    (
        'schema = 1\n[[rules]]\necosystem = "npm"\nname = "p"\nversions = "<1.0.0"\n'
        'action = "deny"\nreason = "CVE\\nline2\\u001bbad"\n'
    ),
    # A1: a prerelease tag on a FULL three-segment version is valid semver and
    # MUST compile + be accepted by the real filter (the rejected sibling is the
    # prerelease-on-PARTIAL form pinned in the fixture pairs below).
    'schema = 1\n[[rules]]\necosystem = "npm"\nname = "evil"\nversions = ">=1.0.0-rc.1"\naction = "deny"\n',
    'schema = 1\n[[rules]]\necosystem = "npm"\nname = "evil2"\nversions = "1.2.3-alpha"\naction = "deny"\n',
]

# A1 semver subset, pinned as accept/reject fixture PAIRS so the npm validator
# stays sound even when node is unavailable at CI time. Anything the compiler's
# validate_range ACCEPTS must be a genuine subset of real semver.validRange
# {includePrerelease:true, loose:true}; the REJECT side is the class real semver
# returns null for (a prerelease tag on a partial 1-/2-segment version), which,
# if emitted, makes the filter throw and fail-close ALL npm.
_NPM_RANGE_ACCEPT = [
    "1.2.3-alpha",
    "1.2.3-rc.1+build",
    ">=1.0.0-beta.1",
    "^1.2.3-alpha",
    "1.2.x",
    "1.x",
    "x",
    "1",
    "1.2",
    "1+build",
    "1.2+build",
    "<2 || >3",
]
_NPM_RANGE_REJECT = [
    "1-alpha",
    "1.2-rc.1",
    ">=1-0",
    "^1.2-alpha",
    "v1-rc.1",
    "1.x.3",
    "x.2.3",
    "*.2.3",
    ">=1.x.3",
]

_NODE = shutil.which("node")


@pytest.mark.skipif(
    _NODE is None or not _PLUGIN_SRC.exists() or not _PLUGIN_TSC.exists(),
    reason="node and/or the Verdaccio plugin build toolchain unavailable",
)
def test_emitted_npm_yaml_accepted_by_filter():
    """Every emitted npm YAML must compile under the real filter compilePolicy.

    Builds policy.ts into a throwaway dir; reverts all churn. A YAML the filter
    rejects would throw -> {ok:false} -> ALL npm fails closed, so this is the
    contract the A1/A2 fixes must satisfy.
    """
    yamls = [_compile(p).npm_yaml for p in _NPM_POLICIES]

    with tempfile.TemporaryDirectory() as d:
        # build policy.ts using the plugin's own tsc + tsconfig, output to tmp.
        proc = subprocess.run(
            [str(_PLUGIN_TSC), "-p", "tsconfig.json", "--outDir", d],
            cwd=str(_PLUGIN_DIR),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"tsc failed: {proc.stderr or proc.stdout}"
        policy_js = Path(d) / "policy.js"
        assert policy_js.exists(), "tsc did not emit policy.js"

        driver = Path(d) / "driver.cjs"
        driver.write_text(
            "const p = require(%r);\n"
            "const yaml = require('js-yaml');\n"
            "const fs = require('fs');\n"
            "const logger = {info(){},warn(){},error(){},debug(){}};\n"
            "const yamls = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
            "for (const y of yamls) {\n"
            "  const doc = yaml.load(y);\n"
            "  p.compilePolicy(doc, logger);\n"  # throws on a rejected artifact
            "}\n"
            "process.stdout.write('ok');\n" % str(policy_js)
        )
        env = {"NODE_PATH": str(_PLUGIN_NODE_MODULES)}
        import os

        run_env = {**os.environ, **env}
        result = subprocess.run(
            [_NODE, str(driver)],
            input=json.dumps(yamls),
            capture_output=True,
            text=True,
            env=run_env,
        )
        assert result.returncode == 0, (
            f"filter compilePolicy rejected an emitted artifact: {result.stderr}"
        )
        assert result.stdout == "ok"


@pytest.mark.skipif(
    _NODE is None or not _PLUGIN_SRC.exists() or not _PLUGIN_TSC.exists(),
    reason="node and/or the Verdaccio plugin build toolchain unavailable",
)
def test_emitted_npm_reason_round_trips_exactly():
    """A reason with control chars must round-trip through js-yaml to the EXACT
    original (no folding, no throw) -> the right package stays blocked (A2)."""
    text = (
        'schema = 1\n[[rules]]\necosystem = "npm"\nname = "p"\nversions = "<1.0.0"\n'
        'action = "deny"\nreason = "CVE\\nline2\\u001bbad"\n'
    )
    npm_yaml = _compile(text).npm_yaml

    with tempfile.TemporaryDirectory() as d:
        import os

        driver = Path(d) / "rt.cjs"
        driver.write_text(
            "const yaml = require('js-yaml');\n"
            "const fs = require('fs');\n"
            "const doc = yaml.load(fs.readFileSync(0, 'utf8'));\n"
            "process.stdout.write(JSON.stringify(doc.blocked.packages[0].reason));\n"
        )
        run_env = {**os.environ, "NODE_PATH": str(_PLUGIN_NODE_MODULES)}
        result = subprocess.run(
            [_NODE, str(driver)],
            input=npm_yaml,
            capture_output=True,
            text=True,
            env=run_env,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == "CVE\nline2\x1bbad"


# representative policies exercising the pypi emission paths.
_PYPI_POLICIES = [
    'schema = 1\n[[rules]]\necosystem = "pypi"\nname = "urllib3"\nversions = ">=2"\naction = "deny"\n',
    (
        'schema = 1\n'
        '[[rules]]\necosystem = "pypi"\nname = "pyyaml"\nversions = "<5.4"\naction = "deny"\n'
        '[[rules]]\necosystem = "pypi"\nname = "pyyaml"\nversions = ">=7"\naction = "deny"\n'
    ),
    'schema = 1\n[[rules]]\necosystem = "pypi"\nname = "some-pkg"\naction = "deny"\n',
    'schema = 1\n[[rules]]\necosystem = "pypi"\nname = "Foo_Bar"\nversions = ">=2"\naction = "deny"\n',
    (
        'schema = 1\n[defaults]\naction = "allow"\n'
        '[defaults.ecosystems.pypi]\naction = "deny"\n'
        '[[rules]]\necosystem = "pypi"\nname = "good-pkg"\naction = "allow"\n'
    ),
]


def _devpi_parse_constraints():
    """Return devpi's real parse_constraints if its deps are importable, else None."""
    try:
        import pkg_resources  # noqa: F401  (needed by devpi's parser)
    except Exception:
        return None
    import importlib.util

    main_py = (
        _REPO_ROOT
        / "devpi"
        / "artea_devpi_policy"
        / "src"
        / "artea_devpi_policy"
        / "main.py"
    )
    if not main_py.exists():
        return None
    spec = importlib.util.spec_from_file_location("_artea_devpi_main", main_py)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return getattr(mod, "parse_constraints", None)


def test_emitted_constraints_accepted_by_devpi_contract():
    """Every emitted constraints text must be accepted by devpi's parse_constraints
    (real if importable, else the stdlib contract MockDevpi uses)."""
    real = _devpi_parse_constraints()
    for policy in _PYPI_POLICIES:
        constraints = _compile(policy).pypi_constraints
        if real is not None:
            real(constraints)  # raises on a rejected artifact
        else:
            parse_constraints_contract(constraints)  # stdlib contract, no raise


def test_stdlib_constraint_contract_rejects_bad_inputs():
    """Sanity-pin the stdlib contract: a malformed name, a bad specifier, and a
    repeated project name must all be rejected (so MockDevpi 400s on them)."""
    with pytest.raises(ValueError):
        parse_constraints_contract("foo bar<2\n")
    with pytest.raises(ValueError):
        parse_constraints_contract("foo<<2\n")
    with pytest.raises(ValueError, match="already exists"):
        parse_constraints_contract("foo<2\nfoo>=5\n")
    # a valid one does not raise
    parse_constraints_contract("urllib3<2\npyyaml>=5.4,<7\n*\n")


def test_npm_validator_pins_semver_subset():
    """A1: the npm validator accepts every _NPM_RANGE_ACCEPT and rejects every
    _NPM_RANGE_REJECT. The reject side is the prerelease-on-partial / wildcard-in-
    a-non-trailing-position class that real semver.validRange returns null for; if
    the validator accepted one the compiler would emit a range the filter throws on
    and fail-close ALL npm. Node-free, so it pins the contract at CI time."""
    from policy_sync.adapters import NpmAdapter
    from policy_sync.policy_model import PolicyError

    npm = NpmAdapter()
    for expr in _NPM_RANGE_ACCEPT:
        npm.validate_range(expr)  # must not raise
    for expr in _NPM_RANGE_REJECT:
        with pytest.raises(PolicyError):
            npm.validate_range(expr)


@pytest.mark.skipif(
    _NODE is None or not (_PLUGIN_NODE_MODULES / "semver").exists(),
    reason="node and/or the semver lib (Verdaccio plugin node_modules) unavailable",
)
def test_npm_accept_set_is_real_semver_subset():
    """A1 differential: every range the npm validator accepts must be accepted by
    the REAL semver.validRange {includePrerelease:true, loose:true}, and every
    range it rejects must be null under real semver too (confirming the subset is
    tight, not merely safe). Uses the plugin's own bundled semver; no source/lock
    is touched."""
    import os

    with tempfile.TemporaryDirectory() as d:
        driver = Path(d) / "semver_check.cjs"
        driver.write_text(
            "const semver = require('semver');\n"
            "const fs = require('fs');\n"
            "const {accept, reject} = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
            "const opts = {includePrerelease:true, loose:true};\n"
            "const out = {accept_rejected_by_semver: [], reject_accepted_by_semver: []};\n"
            "for (const r of accept) if (semver.validRange(r, opts) === null) out.accept_rejected_by_semver.push(r);\n"
            "for (const r of reject) if (semver.validRange(r, opts) !== null) out.reject_accepted_by_semver.push(r);\n"
            "process.stdout.write(JSON.stringify(out));\n"
        )
        run_env = {**os.environ, "NODE_PATH": str(_PLUGIN_NODE_MODULES)}
        result = subprocess.run(
            [_NODE, str(driver)],
            input=json.dumps({"accept": _NPM_RANGE_ACCEPT, "reject": _NPM_RANGE_REJECT}),
            capture_output=True,
            text=True,
            env=run_env,
        )
        assert result.returncode == 0, result.stderr
        out = json.loads(result.stdout)
        assert out["accept_rejected_by_semver"] == [], (
            f"validator accepts ranges real semver REJECTS (fail-close): {out['accept_rejected_by_semver']}"
        )
        assert out["reject_accepted_by_semver"] == [], (
            f"validator rejects ranges real semver ACCEPTS (over-strict pins): {out['reject_accepted_by_semver']}"
        )
