import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from policy_sync.adapters import NpmAdapter, PypiAdapter
from policy_sync.policy_model import PolicyError

npm = NpmAdapter()
pypi = PypiAdapter()

# The repo root, used to locate the Verdaccio plugin's bundled semver for the
# OPTIONAL node-gated cross-check.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEMVER_DIR = (
    _REPO_ROOT
    / "verdaccio"
    / "plugins"
    / "verdaccio-filter-artea"
    / "node_modules"
    / "semver"
)

# (range, expected_valid) fixture pairs that PIN the npm range validator without
# needing node at CI time. Every reject case here is one semver.validRange(r,
# {includePrerelease:true, loose:true}) actually returns null for (verified live)
# and the old all-positions wildcard grammar wrongly ACCEPTED -> emitting it made
# the filter throw -> ALL npm fails closed. The optional node-gated test below
# re-checks these against the real semver.
NPM_RANGE_FIXTURES = [
    # trailing wildcards are accepted by semver
    ("x", True),
    ("*", True),
    ("1.x", True),
    ("1.X", True),
    ("1.2.x", True),
    ("1.x.x", True),
    ("v1.x", True),
    ("~1.x", True),
    ("^1.2.x", True),
    (">=1.x", True),
    # plain ranges / versions
    ("<2", True),
    ("1.2.3", True),
    ("1.0.0 - 2.0.0", True),
    # a wildcard followed by a concrete segment is REJECTED by semver
    ("1.x.3", False),
    ("x.2.3", False),
    ("*.2.3", False),
    (">=1.x.3", False),
    ("X.2", False),
    ("1.X.3", False),
    ("0.x.0", False),
]


# ----------------------------------------------------------------------- npm


def test_npm_normalize_lowercase():
    assert npm.normalize_name("Event-Stream") == "event-stream"
    assert npm.normalize_name("@Scope/Pkg") == "@scope/pkg"


def test_npm_namespace_adds_at():
    assert npm.normalize_namespace("evil-corp") == "@evil-corp"
    assert npm.normalize_namespace("@evil-corp") == "@evil-corp"


@pytest.mark.parametrize(
    "r",
    ["<2", ">=1 <2", "1.2.x", "<2 || >3", "1.3.0", "*", "^1.2.3", "~1.2",
     "1.x", "1.x.x", "x", "~1.x", "^1.2.x", "v1.x"],
)
def test_npm_valid_ranges(r):
    npm.validate_range(r)  # no raise


@pytest.mark.parametrize("r", ["not-a-range !!", "<<2", "1.2.3.4.5 garbage", ""])
def test_npm_invalid_range_rejected(r):
    with pytest.raises(PolicyError, match="invalid semver range"):
        npm.validate_range(r)


@pytest.mark.parametrize("r", ["1.x.3", "x.2.3", "*.2.3", ">=1.x.3", "X.2", "1.X.3", "0.x.0"])
def test_npm_trailing_wildcard_only(r):
    # a wildcard may only TRAIL; a concrete segment after a wildcard is rejected
    # by semver.validRange, so the validator must reject it too (A1) or the filter
    # would throw -> {ok:false} -> ALL npm fails closed.
    with pytest.raises(PolicyError, match="invalid semver range"):
        npm.validate_range(r)


@pytest.mark.parametrize("r,valid", NPM_RANGE_FIXTURES)
def test_npm_range_fixture_pairs_pin_validator(r, valid):
    # pins the subset validator against the verified semver accept/reject set
    # without needing node (decision 4).
    if valid:
        npm.validate_range(r)  # no raise
    else:
        with pytest.raises(PolicyError, match="invalid semver range"):
            npm.validate_range(r)


@pytest.mark.skipif(
    shutil.which("node") is None or not (_SEMVER_DIR / "index.js").exists(),
    reason="node and/or bundled semver unavailable",
)
def test_npm_range_fixtures_match_real_semver():
    # OPTIONAL cross-check: every fixture pair agrees with the REAL
    # semver.validRange(r, {includePrerelease:true, loose:true}). Runs in a
    # throwaway dir; touches no plugin source/package.json/lock.
    script = (
        "const semver = require(%r);\n"
        "const OPTS = {includePrerelease:true, loose:true};\n"
        "const fs = require('fs');\n"
        "const cases = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
        "const out = cases.map(([r]) => semver.validRange(r, OPTS) !== null);\n"
        "process.stdout.write(JSON.stringify(out));\n"
    ) % str(_SEMVER_DIR)
    import json

    with tempfile.TemporaryDirectory() as d:
        mjs = Path(d) / "check.cjs"
        mjs.write_text(script)
        proc = subprocess.run(
            ["node", str(mjs)],
            input=json.dumps(NPM_RANGE_FIXTURES),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        real = json.loads(proc.stdout)
    expected = [valid for _r, valid in NPM_RANGE_FIXTURES]
    assert real == expected


@pytest.mark.parametrize("r,exact", [("1.2.3", True), ("v1.2.3", True), ("=1.2.3", True),
                                     ("<2", False), ("1.2.x", False), ("*", False),
                                     (">=1 <2", False), ("^1.2.3", False)])
def test_npm_is_exact(r, exact):
    assert npm.is_exact(r) is exact


def test_npm_exact_value_strips_prefix():
    assert npm.exact_value("1.2.3") == "1.2.3"
    assert npm.exact_value("v1.2.3") == "1.2.3"
    assert npm.exact_value("=1.2.3") == "1.2.3"


def test_npm_complement_builds_valid_range():
    npm.validate_range("<1.2.3 || >1.2.3")  # no raise


# ---------------------------------------------------------------------- pypi


def test_pypi_normalize_pep503():
    assert pypi.normalize_name("Foo_Bar.Baz") == "foo-bar-baz"
    assert pypi.normalize_name("PyYAML") == "pyyaml"


@pytest.mark.parametrize("name", ["foo", "foo-bar", "a", "foo-bar-baz", "Foo_Bar", "x0"])
def test_pypi_name_shape_accepted(name):
    pypi.normalize_name(name)  # no raise


@pytest.mark.parametrize("name", ["foo bar", "foo#x", "foo\nbar", "", "foo/bar", "-foo", "foo-"])
def test_pypi_name_shape_rejected(name):
    # a malformed name emits a constraint devpi's parse_constraints rejects ->
    # the whole PATCH 400s -> ALL pypi denies fail to freeze (A3).
    with pytest.raises(PolicyError, match="invalid pypi package name"):
        pypi.normalize_name(name)


def test_pypi_no_namespace():
    assert pypi.supports_namespace() is False


@pytest.mark.parametrize("s", ["<2", "==1.2.3", ">=5.4,<7", "!=1.2.3", "~=1.4", "==1.*", "===1.0"])
def test_pypi_valid_specifiers(s):
    pypi.validate_range(s)  # no raise


@pytest.mark.parametrize("s", ["2", "not a spec", "<>1", ""])
def test_pypi_invalid_specifier_rejected(s):
    with pytest.raises(PolicyError, match="invalid PEP 440 specifier"):
        pypi.validate_range(s)


@pytest.mark.parametrize("s,exact", [("==1.2.3", True), (">=2", False),
                                     ("==1.*", False), ("!=1.0", False), ("~=1.4", False)])
def test_pypi_is_exact(s, exact):
    assert pypi.is_exact(s) is exact


def test_pypi_exact_value():
    assert pypi.exact_value("==1.2.3") == "1.2.3"
