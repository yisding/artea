import pytest

from policy_sync.adapters import NpmAdapter, PypiAdapter
from policy_sync.compiler import compile_policy
from policy_sync.devpi import _effective_lines
from policy_sync.policy_model import PolicyError, parse_policy

npm = NpmAdapter()
pypi = PypiAdapter()


def compile_toml(text: str):
    return compile_policy(parse_policy(text.encode("utf-8")))


# ------------------------------------------------------------------ npm emit shape


def test_npm_whole_package_deny_emits_bare_string():
    arts = compile_toml(
        'schema = 1\n[[rules]]\necosystem = "npm"\nname = "event-stream"\naction = "deny"\n'
    )
    assert arts.npm_yaml == (
        'upstream:\n'
        '  min_age: "P0D"\n'
        'blocked:\n'
        '  packages:\n'
        '    - "event-stream"\n'
    )


def test_npm_scope_deny_emits_scopes():
    arts = compile_toml(
        'schema = 1\n[[rules]]\necosystem = "npm"\nnamespace = "evil-corp"\naction = "deny"\n'
    )
    assert '  scopes:\n    - "@evil-corp"\n' in arts.npm_yaml


def test_npm_range_deny_emits_versions_mapping():
    arts = compile_toml(
        'schema = 1\n[[rules]]\necosystem = "npm"\nname = "left-pad"\nversions = "<1.0.0"\naction = "deny"\n'
    )
    assert '    - name: "left-pad"\n      versions: "<1.0.0"\n' in arts.npm_yaml


def test_npm_multiple_ranges_accumulated():
    arts = compile_toml(
        'schema = 1\n'
        '[[rules]]\necosystem = "npm"\nname = "pkg"\nversions = "<1.0.0"\naction = "deny"\n'
        '[[rules]]\necosystem = "npm"\nname = "pkg"\nversions = ">=2.0.0"\naction = "deny"\n'
    )
    assert arts.npm_yaml.count('name: "pkg"') == 2
    assert '"<1.0.0"' in arts.npm_yaml and '">=2.0.0"' in arts.npm_yaml


def test_npm_reason_emitted():
    arts = compile_toml(
        'schema = 1\n[[rules]]\necosystem = "npm"\nname = "p"\nversions = "<1.0.0"\n'
        'action = "deny"\nreason = "CVE-1"\n'
    )
    assert '      reason: "CVE-1"\n' in arts.npm_yaml


def test_npm_emit_is_deterministic_sorted():
    text = (
        'schema = 1\n'
        '[[rules]]\necosystem = "npm"\nname = "zebra"\naction = "deny"\n'
        '[[rules]]\necosystem = "npm"\nname = "alpha"\naction = "deny"\n'
    )
    a = compile_toml(text).npm_yaml
    b = compile_toml(text).npm_yaml
    assert a == b
    assert a.index('"alpha"') < a.index('"zebra"')


def test_npm_empty_policy_emits_upstream_only():
    arts = compile_toml("schema = 1\n")
    assert arts.npm_yaml == 'upstream:\n  min_age: "P0D"\n'


def test_npm_emitted_ranges_validate():
    arts = compile_toml(
        'schema = 1\n[[rules]]\necosystem = "npm"\nname = "p"\nversions = "<1 || >3"\naction = "deny"\n'
    )
    # every emitted versions string must pass the adapter (the filter's contract)
    for line in arts.npm_yaml.splitlines():
        line = line.strip()
        if line.startswith("versions:"):
            npm.validate_range(line.split(":", 1)[1].strip().strip('"'))


# ------------------------------------------------------------- npm allow-wins


def test_whole_package_allow_drops_denies_npm():
    arts = compile_toml(
        'schema = 1\n'
        '[[rules]]\necosystem = "npm"\nname = "p"\naction = "deny"\n'
        '[[rules]]\necosystem = "npm"\nname = "p"\naction = "allow"\n'
    )
    assert '"p"' not in arts.npm_yaml


def test_exact_allow_npm_emits_complement():
    arts = compile_toml(
        'schema = 1\n'
        '[[rules]]\necosystem = "npm"\nname = "p"\naction = "deny"\n'
        '[[rules]]\necosystem = "npm"\nname = "p"\nversions = "1.2.3"\naction = "allow"\n'
    )
    assert '      versions: "<1.2.3 || >1.2.3"\n' in arts.npm_yaml


def test_allow_no_matching_deny_is_noop_npm():
    arts = compile_toml(
        'schema = 1\n[[rules]]\necosystem = "npm"\nname = "p"\naction = "allow"\n'
    )
    assert arts.npm_yaml == 'upstream:\n  min_age: "P0D"\n'


# ------------------------------------------------------------- pypi emit / devpi


def test_pypi_range_deny_emits_complement():
    # devpi reads a constraint as an ALLOW set, so a deny of >=2 must emit the
    # complement <2 (allow only <2 == hide 2.x).
    arts = compile_toml(
        'schema = 1\n[[rules]]\necosystem = "pypi"\nname = "urllib3"\nversions = ">=2"\naction = "deny"\n'
    )
    assert _effective_lines(arts.pypi_constraints) == ["urllib3<2"]


def test_pypi_two_range_denies_combine_into_one_line():
    # allow only [5.4, 7): deny <5.4 + deny >=7 -> complements >=5.4 and <7,
    # combined into ONE devpi constraint (a repeated project name is rejected).
    arts = compile_toml(
        'schema = 1\n'
        '[[rules]]\necosystem = "pypi"\nname = "pyyaml"\nversions = "<5.4"\naction = "deny"\n'
        '[[rules]]\necosystem = "pypi"\nname = "pyyaml"\nversions = ">=7"\naction = "deny"\n'
    )
    assert _effective_lines(arts.pypi_constraints) == ["pyyaml>=5.4,<7"]


def test_pypi_complement_of_each_comparator():
    for deny, allow in [
        (">=2", "<2"),
        (">2", "<=2"),
        ("<2", ">=2"),
        ("<=3", ">3"),
        ("==1.2.3", "!=1.2.3"),
    ]:
        arts = compile_toml(
            f'schema = 1\n[[rules]]\necosystem = "pypi"\nname = "p"\nversions = "{deny}"\naction = "deny"\n'
        )
        assert _effective_lines(arts.pypi_constraints) == [f"p{allow}"]


def test_pypi_uncomplementable_range_rejected():
    # ~= has no single-specifier complement -> validation-reject.
    with pytest.raises(PolicyError, match="cannot be inverted"):
        compile_toml(
            'schema = 1\n[[rules]]\necosystem = "pypi"\nname = "p"\nversions = "~=1.4"\naction = "deny"\n'
        )


def test_pypi_compound_range_deny_rejected():
    # a compound specifier set is not a single complementable comparator.
    with pytest.raises(PolicyError, match="cannot be inverted"):
        compile_toml(
            'schema = 1\n[[rules]]\necosystem = "pypi"\nname = "p"\nversions = ">=5.4,<7"\naction = "deny"\n'
        )


def test_pypi_whole_package_deny_emits_kill():
    arts = compile_toml(
        'schema = 1\n[[rules]]\necosystem = "pypi"\nname = "some-pkg"\naction = "deny"\n'
    )
    assert _effective_lines(arts.pypi_constraints) == ["some-pkg==0"]


def test_pypi_name_normalized_in_emit():
    arts = compile_toml(
        'schema = 1\n[[rules]]\necosystem = "pypi"\nname = "Foo_Bar"\nversions = ">=2"\naction = "deny"\n'
    )
    assert _effective_lines(arts.pypi_constraints) == ["foo-bar<2"]


def test_pypi_default_deny_appends_star():
    arts = compile_toml(
        'schema = 1\n[defaults]\naction = "allow"\n'
        '[defaults.ecosystems.pypi]\naction = "deny"\n'
    )
    assert _effective_lines(arts.pypi_constraints) == ["*"]


def test_pypi_default_deny_with_allow_passthrough():
    arts = compile_toml(
        'schema = 1\n[defaults]\naction = "allow"\n'
        '[defaults.ecosystems.pypi]\naction = "deny"\n'
        '[[rules]]\necosystem = "pypi"\nname = "good-pkg"\naction = "allow"\n'
    )
    assert _effective_lines(arts.pypi_constraints) == ["good-pkg", "*"]


def test_pypi_exact_allow_against_whole_deny_emits_eqeq():
    arts = compile_toml(
        'schema = 1\n'
        '[[rules]]\necosystem = "pypi"\nname = "p"\naction = "deny"\n'
        '[[rules]]\necosystem = "pypi"\nname = "p"\nversions = "==1.2.3"\naction = "allow"\n'
    )
    assert _effective_lines(arts.pypi_constraints) == ["p==1.2.3"]


def test_pypi_empty_policy_emits_empty_text():
    arts = compile_toml("schema = 1\n")
    assert arts.pypi_constraints == ""


def test_pypi_emitted_specifiers_validate():
    # two single-comparator denies combine into one valid PEP 440 set.
    arts = compile_toml(
        'schema = 1\n'
        '[[rules]]\necosystem = "pypi"\nname = "x"\nversions = "<5.4"\naction = "deny"\n'
        '[[rules]]\necosystem = "pypi"\nname = "x"\nversions = ">=7"\naction = "deny"\n'
    )
    for line in _effective_lines(arts.pypi_constraints):
        if line == "*":
            continue
        for i, ch in enumerate(line):
            if ch in "=!<>~":
                pypi.validate_range(line[i:])
                break


def test_whole_package_allow_drops_denies_pypi():
    arts = compile_toml(
        'schema = 1\n'
        '[[rules]]\necosystem = "pypi"\nname = "p"\naction = "deny"\n'
        '[[rules]]\necosystem = "pypi"\nname = "p"\naction = "allow"\n'
    )
    assert arts.pypi_constraints == ""


# ------------------------------------------------------- A2 npm_quote escaping


def test_npm_quote_escapes_newline_in_reason():
    # a newline in a reason must be escaped, not emitted raw (js-yaml would fold
    # a raw newline and could shift the mapping) -> ALL npm fails closed.
    arts = compile_toml(
        'schema = 1\n[[rules]]\necosystem = "npm"\nname = "p"\nversions = "<1.0.0"\n'
        'action = "deny"\nreason = "line1\\nline2"\n'
    )
    line = next(
        ln for ln in arts.npm_yaml.splitlines() if ln.strip().startswith("reason:")
    )
    assert "\\n" in line  # escaped
    # the value itself contains no raw newline (the splitlines proves it is one line)
    assert "line1\\nline2" in line


def test_npm_quote_escapes_nul_and_esc():
    # NUL / ESC must be escaped as \xNN; js-yaml throws on a raw NUL/ESC, which
    # would tear down the whole npm policy at the filter.
    arts = compile_toml(
        'schema = 1\n[[rules]]\necosystem = "npm"\nname = "p"\nversions = "<1.0.0"\n'
        'action = "deny"\nreason = "a\\u0000b\\u001bc"\n'
    )
    reason_line = next(
        ln for ln in arts.npm_yaml.splitlines() if ln.strip().startswith("reason:")
    )
    assert "\\x00" in reason_line
    assert "\\x1b" in reason_line
    assert "\x00" not in reason_line and "\x1b" not in reason_line


def test_npm_quote_newline_in_name_does_not_collapse():
    # name "a\nb" must NOT collapse to "a b" (which would block the wrong package).
    arts = compile_toml(
        'schema = 1\n[[rules]]\necosystem = "npm"\nname = "a\\nb"\naction = "deny"\n'
    )
    assert '"a\\nb"' in arts.npm_yaml
    assert '"a b"' not in arts.npm_yaml


def test_npm_quote_control_free_input_byte_stable():
    # the escaper must not alter control-free output (byte-stability).
    arts = compile_toml(
        'schema = 1\n[[rules]]\necosystem = "npm"\nname = "p"\nversions = "<1.0.0"\n'
        'action = "deny"\nreason = "CVE-2024-1: bad; very/bad"\n'
    )
    assert '      reason: "CVE-2024-1: bad; very/bad"\n' in arts.npm_yaml


# ------------------------------------------------------- A3 malformed pypi name


def test_pypi_malformed_name_fails_compile():
    with pytest.raises(PolicyError, match="invalid pypi package name"):
        compile_toml(
            'schema = 1\n[[rules]]\necosystem = "pypi"\nname = "foo bar"\nversions = ">=2"\naction = "deny"\n'
        )


# ------------------------------------------ A6 contradictory combined complement


def test_pypi_contradictory_combined_rejected():
    # deny >=2 + deny <5 -> complements <2 and >=5 -> combined ">=5,<2", a
    # syntactically valid PEP 440 set whose allow-set is EMPTY -> would silently
    # block the whole package. Reject with a clear message.
    with pytest.raises(PolicyError, match="empty allow-set"):
        compile_toml(
            'schema = 1\n'
            '[[rules]]\necosystem = "pypi"\nname = "p"\nversions = ">=2"\naction = "deny"\n'
            '[[rules]]\necosystem = "pypi"\nname = "p"\nversions = "<5"\naction = "deny"\n'
        )


def test_pypi_non_contradictory_combined_still_compiles():
    # deny <5.4 + deny >=7 -> >=5.4,<7 is a non-empty allow-set; must still compile.
    arts = compile_toml(
        'schema = 1\n'
        '[[rules]]\necosystem = "pypi"\nname = "x"\nversions = "<5.4"\naction = "deny"\n'
        '[[rules]]\necosystem = "pypi"\nname = "x"\nversions = ">=7"\naction = "deny"\n'
    )
    assert _effective_lines(arts.pypi_constraints) == ["x>=5.4,<7"]


# ---------------------------------------------------------------- min_age


def test_min_age_flows_to_artifacts():
    arts = compile_toml('schema = 1\n[upstream]\nmin_age = "P3D"\n')
    assert arts.min_age == "P3D"
    assert '  min_age: "P3D"\n' in arts.npm_yaml


# ------------------------------------------------------------- deferred rejects


def test_npm_default_deny_rejected():
    with pytest.raises(PolicyError, match="default-deny is not supported"):
        compile_toml(
            'schema = 1\n[defaults]\naction = "allow"\n'
            '[defaults.ecosystems.npm]\naction = "deny"\n'
        )


def test_global_default_deny_rejected_for_npm():
    with pytest.raises(PolicyError, match="default-deny is not supported"):
        compile_toml('schema = 1\n[defaults]\naction = "deny"\n')


def test_allow_range_rejected():
    with pytest.raises(PolicyError, match="not a range"):
        compile_toml(
            'schema = 1\n[[rules]]\necosystem = "npm"\nname = "p"\nversions = "<2"\naction = "allow"\n'
        )


def test_exact_allow_against_range_deny_rejected_npm():
    with pytest.raises(PolicyError, match="range carving"):
        compile_toml(
            'schema = 1\n'
            '[[rules]]\necosystem = "npm"\nname = "p"\nversions = "<2.0.0"\naction = "deny"\n'
            '[[rules]]\necosystem = "npm"\nname = "p"\nversions = "1.2.3"\naction = "allow"\n'
        )


def test_exact_allow_against_range_deny_rejected_pypi():
    with pytest.raises(PolicyError, match="range carving"):
        compile_toml(
            'schema = 1\n'
            '[[rules]]\necosystem = "pypi"\nname = "p"\nversions = ">=1"\naction = "deny"\n'
            '[[rules]]\necosystem = "pypi"\nname = "p"\nversions = "==1.2.3"\naction = "allow"\n'
        )


def test_npm_allow_out_of_namespace_rejected():
    with pytest.raises(PolicyError, match="single package out of a namespace deny"):
        compile_toml(
            'schema = 1\n'
            '[[rules]]\necosystem = "npm"\nnamespace = "@scope"\naction = "deny"\n'
            '[[rules]]\necosystem = "npm"\nname = "@scope/pkg"\naction = "allow"\n'
        )


def test_npm_invalid_range_fails_whole_compile():
    with pytest.raises(PolicyError, match="invalid semver range"):
        compile_toml(
            'schema = 1\n[[rules]]\necosystem = "npm"\nname = "p"\nversions = "garbage!!"\naction = "deny"\n'
        )


def test_pypi_namespace_rejected_in_compiler():
    # the model allows namespace structurally; the compiler enforces no-namespace.
    with pytest.raises(PolicyError, match="does not support namespaces"):
        compile_toml(
            'schema = 1\n[[rules]]\necosystem = "pypi"\nnamespace = "x"\naction = "deny"\n'
        )
