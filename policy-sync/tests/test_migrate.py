"""Tests for the legacy -> unified migration generator (policy_sync.migrate).

Stdlib + pytest only. Every generated policy.toml is asserted to (a) parse via
policy_model.parse_policy, (b) compile via compiler.compile_policy, and (c)
contain the expected rules; warnings are checked for inexpressible cases.
"""

import pytest

from policy_sync.compiler import compile_policy
from policy_sync.migrate import (
    generate_policy_toml,
    main,
    _invert_pypi_line,
    _parse_npm_rules,
    _parse_upstream_min_age,
)
from policy_sync.policy_model import parse_policy

NPM_FULL = """\
blocked:
  scopes:
    - "@evil-corp"
    - good-scope
  packages:
    - event-stream
    - name: left-pad
      versions: "1.3.0"
      reason: "sabotage"
    - name: "@acme/utils"
"""

PYPI_FULL = "urllib3<2\nrequests==2.31.0\npkgname==0\n*\n"

UPSTREAM_FULL = "upstream:\n  min_age: P3D\n"


def _write_legacy(tmp_path, npm=None, pypi=None, upstream=None):
    if npm is not None:
        (tmp_path / "npm-rules.yaml").write_text(npm, encoding="utf-8")
    if pypi is not None:
        (tmp_path / "pypi-constraints.txt").write_text(pypi, encoding="utf-8")
    if upstream is not None:
        (tmp_path / "upstream-policy.yaml").write_text(upstream, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- end-to-end


def test_generated_policy_parses_and_compiles(tmp_path):
    _write_legacy(tmp_path, npm=NPM_FULL, pypi=PYPI_FULL, upstream=UPSTREAM_FULL)
    toml = generate_policy_toml(tmp_path)

    policy = parse_policy(toml.encode("utf-8"))  # (a) parses
    arts = compile_policy(policy)  # (b) compiles

    # min_age carried through
    assert arts.min_age == "P3D"
    # npm scopes + packages present
    assert '    - "@evil-corp"' in arts.npm_yaml
    assert '    - "@good-scope"' in arts.npm_yaml
    assert '    - "event-stream"' in arts.npm_yaml
    assert '    - "@acme/utils"' in arts.npm_yaml
    assert 'name: "left-pad"' in arts.npm_yaml
    assert 'versions: "1.3.0"' in arts.npm_yaml
    # pypi: urllib3<2 round-trips, kill sentinel survives
    assert "urllib3<2" in arts.pypi_constraints
    assert "pkgname==0" in arts.pypi_constraints


def test_generated_rules_have_expected_shape(tmp_path):
    _write_legacy(tmp_path, npm=NPM_FULL, pypi=PYPI_FULL, upstream=UPSTREAM_FULL)
    policy = parse_policy(generate_policy_toml(tmp_path).encode("utf-8"))

    npm_ns = {r.namespace for r in policy.rules if r.ecosystem == "npm" and r.namespace}
    assert npm_ns == {"@evil-corp", "@good-scope"}

    left = [r for r in policy.rules if r.name == "left-pad"]
    assert len(left) == 1
    assert left[0].versions == "1.3.0"
    assert left[0].reason == "sabotage"
    assert left[0].action.value == "deny"

    urllib3 = [r for r in policy.rules if r.name == "urllib3"]
    assert len(urllib3) == 1
    assert urllib3[0].versions == ">=2"  # complement of the legacy allow <2


# ----------------------------------------------------------------- round-trip


def test_pypi_single_comparator_round_trips(tmp_path):
    _write_legacy(tmp_path, pypi="urllib3<2\n")
    arts = compile_policy(parse_policy(generate_policy_toml(tmp_path).encode("utf-8")))
    # the legacy allow-constraint must reappear verbatim after migrate+compile.
    assert arts.pypi_constraints == "urllib3<2\n"


def test_pypi_lower_bound_round_trips(tmp_path):
    _write_legacy(tmp_path, pypi="flask>=1.0\n")
    arts = compile_policy(parse_policy(generate_policy_toml(tmp_path).encode("utf-8")))
    assert arts.pypi_constraints == "flask>=1.0\n"


# ------------------------------------------------------------------ warnings


def test_warns_on_star_default_deny(tmp_path, capsys):
    _write_legacy(tmp_path, pypi="*\n")
    generate_policy_toml(tmp_path)
    err = capsys.readouterr().err
    assert "default-deny" in err
    assert "defaults.ecosystems.pypi" in err


def test_warns_on_uninvertible_lines(tmp_path, capsys):
    _write_legacy(tmp_path, pypi="requests==2.31.0\npyyaml>=5.4,<7\n")
    policy = parse_policy(generate_policy_toml(tmp_path).encode("utf-8"))
    err = capsys.readouterr().err
    assert "could not invert" in err
    # neither uninvertible line produced a rule
    assert not [r for r in policy.rules if r.ecosystem == "pypi"]


def test_kill_sentinel_becomes_whole_package_deny(tmp_path, capsys):
    _write_legacy(tmp_path, pypi="dead-pkg==0\n")
    policy = parse_policy(generate_policy_toml(tmp_path).encode("utf-8"))
    err = capsys.readouterr().err
    assert "kill sentinel" in err
    dead = [r for r in policy.rules if r.name == "dead-pkg"]
    assert len(dead) == 1
    assert dead[0].versions is None
    assert dead[0].action.value == "deny"


def test_warns_when_files_missing(tmp_path, capsys):
    # empty dir -> all three files absent
    toml = generate_policy_toml(tmp_path)
    err = capsys.readouterr().err
    assert "npm-rules.yaml not found" in err
    assert "pypi-constraints.txt not found" in err
    assert "upstream-policy.yaml not found" in err
    # still produces a valid minimal default-allow policy
    policy = parse_policy(toml.encode("utf-8"))
    assert policy.rules == ()
    assert policy.defaults.action.value == "allow"


# -------------------------------------------------------------- seed defaults


def test_real_seed_files_migrate_to_empty_default_allow():
    from pathlib import Path

    repo_policy = Path(__file__).resolve().parents[2] / "policy"
    toml = generate_policy_toml(repo_policy)
    policy = parse_policy(toml.encode("utf-8"))
    compile_policy(policy)  # must compile
    # the shipped seeds block nothing -> no rules, default allow, P0D dropped.
    assert policy.rules == ()
    assert policy.defaults.action.value == "allow"
    assert policy.min_age == "P0D"
    assert "[upstream]" not in toml  # P0D is the default, omitted


# ------------------------------------------------------------------- parsers


def test_parse_npm_rules_bare_and_mapping():
    scopes, packages = _parse_npm_rules(NPM_FULL)
    assert scopes == ["@evil-corp", "good-scope"]
    names = [p["name"] for p in packages]
    assert names == ["event-stream", "left-pad", "@acme/utils"]
    lp = next(p for p in packages if p["name"] == "left-pad")
    assert lp["versions"] == "1.3.0"
    assert lp["reason"] == "sabotage"


def test_parse_npm_rules_empty_lists():
    scopes, packages = _parse_npm_rules("blocked:\n  scopes: []\n  packages: []\n")
    assert scopes == []
    assert packages == []


def test_parse_upstream_min_age():
    assert _parse_upstream_min_age("upstream:\n  min_age: P7D\n") == "P7D"
    assert _parse_upstream_min_age("upstream:\n  minimum_age: PT72H\n") == "PT72H"
    assert _parse_upstream_min_age("# nothing\n") is None


@pytest.mark.parametrize(
    "line,expect_rule,expect_warn",
    [
        ("urllib3<2", True, False),
        ("flask>=1.0", True, False),
        ("pkg==0", True, True),  # kill sentinel: rule + warn
        ("requests==2.31.0", False, True),
        ("pyyaml>=5.4,<7", False, True),
        ("*", False, True),
        ("foo bar==1", False, True),  # invalid name
    ],
)
def test_invert_pypi_line_table(line, expect_rule, expect_warn):
    rule, warning = _invert_pypi_line(line)
    assert (rule is not None) == expect_rule
    assert (warning is not None) == expect_warn


# ---------------------------------------------------------------------- CLI


def test_main_writes_to_out(tmp_path, capsys):
    _write_legacy(tmp_path, npm=NPM_FULL, pypi="urllib3<2\n", upstream=UPSTREAM_FULL)
    out = tmp_path / "policy.toml"
    rc = main([str(tmp_path), "--out", str(out)])
    assert rc == 0
    assert out.is_file()
    compile_policy(parse_policy(out.read_bytes()))  # generated file compiles
    err = capsys.readouterr().err
    assert "review" in err


def test_main_prints_to_stdout(tmp_path, capsys):
    _write_legacy(tmp_path, pypi="urllib3<2\n")
    rc = main([str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "schema = 1" in out
    assert "urllib3" in out


def test_main_rejects_missing_dir(tmp_path):
    rc = main([str(tmp_path / "nope")])
    assert rc == 2
