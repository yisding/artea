import pytest

from policy_sync.policy_model import Action, PolicyError, parse_policy


def parse(text: str):
    return parse_policy(text.encode("utf-8"))


def test_parses_minimal_policy():
    p = parse(
        """
        schema = 1
        [[rules]]
        ecosystem = "npm"
        name = "Event-Stream"
        action = "deny"
        """
    )
    assert p.schema == 1
    assert p.defaults.action is Action.ALLOW
    assert p.min_age == "P0D"
    assert len(p.rules) == 1
    r = p.rules[0]
    assert r.ecosystem == "npm"
    assert r.name == "Event-Stream"  # model keeps source form; adapter normalizes
    assert r.action is Action.DENY
    assert r.source == "curated"


def test_action_defaults_to_deny():
    p = parse(
        """
        schema = 1
        [[rules]]
        ecosystem = "npm"
        name = "left-pad"
        """
    )
    assert p.rules[0].action is Action.DENY


def test_invalid_toml_rejected():
    with pytest.raises(PolicyError, match="not valid TOML"):
        parse("schema = = 1")


def test_non_utf8_rejected():
    with pytest.raises(PolicyError, match="not valid UTF-8"):
        parse_policy(b"\xff\xfe schema = 1")


def test_missing_schema_rejected():
    with pytest.raises(PolicyError, match="schema must be 1"):
        parse('[[rules]]\necosystem = "npm"\nname = "x"\n')


def test_wrong_schema_rejected():
    with pytest.raises(PolicyError, match="schema must be 1"):
        parse("schema = 2\n")


def test_unknown_top_level_key_rejected():
    with pytest.raises(PolicyError, match="unknown top-level key"):
        parse("schema = 1\nbogus = true\n")


def test_rule_both_name_and_namespace_rejected():
    with pytest.raises(PolicyError, match="exactly one of 'name' or 'namespace'"):
        parse(
            'schema = 1\n[[rules]]\necosystem = "npm"\nname = "x"\nnamespace = "@y"\n'
        )


def test_rule_neither_rejected():
    with pytest.raises(PolicyError, match="exactly one of 'name' or 'namespace'"):
        parse('schema = 1\n[[rules]]\necosystem = "npm"\n')


def test_versions_with_namespace_rejected():
    with pytest.raises(PolicyError, match="'versions' is only valid with 'name'"):
        parse(
            'schema = 1\n[[rules]]\necosystem = "npm"\nnamespace = "@x"\nversions = "<2"\n'
        )


def test_bad_rule_action_rejected():
    with pytest.raises(PolicyError, match="action must be 'allow' or 'deny'"):
        parse('schema = 1\n[[rules]]\necosystem = "npm"\nname = "x"\naction = "warn"\n')


def test_bad_defaults_action_rejected():
    with pytest.raises(PolicyError, match="defaults.action must be 'allow' or 'deny'"):
        parse('schema = 1\n[defaults]\naction = "warn"\n')


def test_bad_per_ecosystem_default_rejected():
    with pytest.raises(PolicyError, match="defaults.ecosystems.pypi.action"):
        parse(
            'schema = 1\n[defaults]\naction = "allow"\n'
            '[defaults.ecosystems.pypi]\naction = "nope"\n'
        )


def test_per_ecosystem_default_parsed():
    p = parse(
        'schema = 1\n[defaults]\naction = "allow"\n'
        '[defaults.ecosystems.pypi]\naction = "deny"\n'
    )
    assert p.defaults.for_ecosystem("pypi") is Action.DENY
    assert p.defaults.for_ecosystem("npm") is Action.ALLOW


def test_unknown_ecosystem_rejected():
    with pytest.raises(PolicyError, match="unknown ecosystem 'maven'"):
        parse('schema = 1\n[[rules]]\necosystem = "maven"\nname = "x"\n')


def test_min_age_parsed_and_validated():
    p = parse('schema = 1\n[upstream]\nmin_age = "P3D"\n')
    assert p.min_age == "P3D"


def test_osv_malicious_packages_defaults_disabled():
    p = parse("schema = 1\n")
    assert p.osv_malicious_packages is False


def test_osv_malicious_packages_parsed():
    p = parse("schema = 1\n[osv]\nmalicious_packages = true\n")
    assert p.osv_malicious_packages is True


def test_bad_osv_malicious_packages_rejected():
    with pytest.raises(PolicyError, match="osv.malicious_packages must be a boolean"):
        parse('schema = 1\n[osv]\nmalicious_packages = "yes"\n')


def test_unknown_osv_key_rejected():
    with pytest.raises(PolicyError, match="osv: unknown key"):
        parse("schema = 1\n[osv]\nbogus = true\n")


def test_min_age_alias_minimum_age():
    p = parse('schema = 1\n[upstream]\nminimum_age = "PT12H"\n')
    assert p.min_age == "PT12H"


def test_bad_min_age_rejected():
    with pytest.raises(PolicyError, match="ISO 8601 duration"):
        parse('schema = 1\n[upstream]\nmin_age = "3 days"\n')


def test_non_string_min_age_rejected():
    with pytest.raises(PolicyError, match="ISO 8601 duration string"):
        parse("schema = 1\n[upstream]\nmin_age = 3\n")


def test_expired_rule_dropped():
    p = parse(
        'schema = 1\n[[rules]]\necosystem = "npm"\nname = "x"\n'
        'expires = "2000-01-01T00:00:00Z"\n'
    )
    assert p.rules == ()


def test_future_expires_kept():
    p = parse(
        'schema = 1\n[[rules]]\necosystem = "npm"\nname = "x"\n'
        'expires = "2999-01-01T00:00:00Z"\n'
    )
    assert len(p.rules) == 1


def test_bad_expires_rejected():
    with pytest.raises(PolicyError, match="RFC 3339"):
        parse(
            'schema = 1\n[[rules]]\necosystem = "npm"\nname = "x"\n'
            'expires = "not-a-date"\n'
        )


def test_toml_datetime_expires_accepted():
    # TOML offset-datetime literal is parsed to a datetime by tomllib.
    p = parse(
        "schema = 1\n[[rules]]\necosystem = \"npm\"\nname = \"x\"\n"
        "expires = 2999-01-01T00:00:00Z\n"
    )
    assert len(p.rules) == 1


def test_star_as_package_name_rejected():
    with pytest.raises(PolicyError, match="reserved sentinel"):
        parse('schema = 1\n[[rules]]\necosystem = "pypi"\nname = "*"\n')


# ---------------------------------------------- A4 unknown keys at every level


def test_unknown_rule_key_rejected():
    # the 'verisons' typo must be a structural error, NOT a silent whole-package
    # deny (which would block the wrong thing).
    with pytest.raises(PolicyError, match="unknown key verisons"):
        parse(
            'schema = 1\n[[rules]]\necosystem = "npm"\nname = "left-pad"\nverisons = "<2"\n'
        )


def test_unknown_rule_key_rejected_generic():
    with pytest.raises(PolicyError, match="rule 0: unknown key"):
        parse('schema = 1\n[[rules]]\necosystem = "npm"\nname = "x"\nbogus = true\n')


def test_unknown_defaults_key_rejected():
    with pytest.raises(PolicyError, match="defaults: unknown key"):
        parse('schema = 1\n[defaults]\naction = "allow"\nbogus = true\n')


def test_unknown_per_ecosystem_key_rejected():
    with pytest.raises(PolicyError, match=r"defaults.ecosystems.pypi: unknown key"):
        parse(
            'schema = 1\n[defaults]\naction = "allow"\n'
            '[defaults.ecosystems.pypi]\naction = "deny"\nbogus = true\n'
        )
