"""In-memory model of the unified policy + parse from the TOML source file.

The unified source policy is a single TOML file (`policy.toml`) parsed with the
stdlib `tomllib` — no new dependency, the stdlib-only invariant is preserved.
This module is ecosystem-agnostic: it does the structural validation that does
not need a version comparator (shape, required fields, mutual exclusivity,
expiry, the ISO 8601 duration check for upstream.min_age). Version-range parsing
and precedence resolution live in adapters.py / compiler.py, so adapter-specific
errors surface there.

Any structural problem raises PolicyError. A single PolicyError fails the whole
sync, which keeps the previously applied policy in effect (last-known-good).
"""

import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

# the set of ecosystems with a registered adapter; kept here (not imported from
# adapters.py) to avoid an import cycle — adapters.py validates the actual range
# dialects, the model only needs to know which ids are legal.
KNOWN_ECOSYSTEMS = ("npm", "pypi")

# ports the Verdaccio filter's ISO_DURATION_RE (policy.ts) so policy-sync accepts
# exactly the same upstream.min_age strings the filter does.
ISO_DURATION_RE = re.compile(
    r"^P(?:(\d+(?:\.\d+)?)W)?(?:(\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)

DEFAULT_MIN_AGE = "P0D"


class PolicyError(Exception):
    """The single structural-error type for every rejected unified-policy case.

    One message per failure, actionable, naming the offending rule where it can.
    """


class Action(str, Enum):
    DENY = "deny"
    ALLOW = "allow"


@dataclass(frozen=True)
class Rule:
    ecosystem: str
    name: str | None  # exactly one of name / namespace is set
    namespace: str | None
    versions: str | None  # native dialect; only valid with name
    action: Action
    reason: str | None
    source: str
    expires: str | None  # RFC 3339; expired rules are dropped at parse time


@dataclass(frozen=True)
class Defaults:
    action: Action  # global baseline
    ecosystems: dict[str, Action] = field(default_factory=dict)

    def for_ecosystem(self, ecosystem: str) -> Action:
        return self.ecosystems.get(ecosystem, self.action)


@dataclass(frozen=True)
class Policy:
    schema: int
    defaults: Defaults
    rules: tuple[Rule, ...]
    min_age: str
    osv_malicious_packages: bool


def _validate_min_age(raw: object) -> str:
    """Validate an upstream.min_age value, reusing the filter's three messages."""
    if raw is None:
        return DEFAULT_MIN_AGE
    if not isinstance(raw, str):
        raise PolicyError(
            '"upstream.min_age" must be an ISO 8601 duration string such as "P3D" or "PT72H"'
        )
    value = raw.strip()
    match = ISO_DURATION_RE.match(value)
    if not match or all(g is None for g in match.groups()):
        raise PolicyError(
            '"upstream.min_age" must use ISO 8601 duration syntax such as "P3D" or "PT72H"'
        )
    # every component is a non-negative number by the regex; a bare "P"/"PT" is
    # rejected above. Negative durations cannot be expressed, but keep the guard
    # symmetric with the filter's non-negative check.
    if any(g is not None and float(g) < 0 for g in match.groups()):
        raise PolicyError('"upstream.min_age" must be a non-negative duration')
    return value


def _parse_action(raw: object, where: str) -> Action:
    if not isinstance(raw, str):
        raise PolicyError(f"{where} must be 'allow' or 'deny'")
    try:
        return Action(raw)
    except ValueError:
        raise PolicyError(f"{where} must be 'allow' or 'deny'") from None


_ALLOWED_DEFAULTS_KEYS = {"action", "ecosystems"}
_ALLOWED_ECOSYSTEM_OVERRIDE_KEYS = {"action"}


def _reject_unknown_keys(raw: dict, allowed: set[str], where: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        keys = ", ".join(sorted(unknown))
        allowed_s = ", ".join(sorted(allowed))
        raise PolicyError(f"{where}: unknown key {keys}; allowed: {allowed_s}")


def _parse_defaults(raw: object) -> Defaults:
    if raw is None:
        return Defaults(action=Action.ALLOW)
    if not isinstance(raw, dict):
        raise PolicyError("'defaults' must be a table")
    _reject_unknown_keys(raw, _ALLOWED_DEFAULTS_KEYS, "defaults")
    action = _parse_action(raw.get("action", "allow"), "defaults.action")
    eco_raw = raw.get("ecosystems")
    ecosystems: dict[str, Action] = {}
    if eco_raw is not None:
        if not isinstance(eco_raw, dict):
            raise PolicyError("'defaults.ecosystems' must be a table")
        for eco, override in eco_raw.items():
            if not isinstance(override, dict):
                raise PolicyError(f"defaults.ecosystems.{eco} must be a table with an 'action'")
            _reject_unknown_keys(
                override, _ALLOWED_ECOSYSTEM_OVERRIDE_KEYS, f"defaults.ecosystems.{eco}"
            )
            ecosystems[eco] = _parse_action(
                override.get("action", "allow"), f"defaults.ecosystems.{eco}.action"
            )
    return Defaults(action=action, ecosystems=ecosystems)


_ALLOWED_RULE_KEYS = {
    "ecosystem",
    "name",
    "namespace",
    "versions",
    "action",
    "reason",
    "source",
    "expires",
}


def _parse_rule(raw: object, index: int) -> Rule | None:
    """Parse one [[rules]] table. Returns None for an already-expired rule."""
    where = f"rule {index}"
    if not isinstance(raw, dict):
        raise PolicyError(f"{where}: must be a table")

    _reject_unknown_keys(raw, _ALLOWED_RULE_KEYS, where)

    ecosystem = raw.get("ecosystem")
    if not isinstance(ecosystem, str) or not ecosystem:
        raise PolicyError(f"{where}: 'ecosystem' is required")
    if ecosystem not in KNOWN_ECOSYSTEMS:
        raise PolicyError(f"{where}: unknown ecosystem '{ecosystem}'; no adapter registered")

    name = raw.get("name")
    namespace = raw.get("namespace")
    if (name is None) == (namespace is None):
        raise PolicyError(f"{where}: exactly one of 'name' or 'namespace' is required")
    if name is not None and (not isinstance(name, str) or not name):
        raise PolicyError(f"{where}: 'name' must be a non-empty string")
    if namespace is not None and (not isinstance(namespace, str) or not namespace):
        raise PolicyError(f"{where}: 'namespace' must be a non-empty string")
    if name == "*" or namespace == "*":
        raise PolicyError(
            f"{where}: '*' is a reserved sentinel and cannot be used as a package name"
        )

    versions = raw.get("versions")
    if versions is not None:
        if namespace is not None:
            raise PolicyError(f"{where}: 'versions' is only valid with 'name'")
        if not isinstance(versions, str) or not versions:
            raise PolicyError(f"{where}: 'versions' must be a non-empty string")

    action = _parse_action(raw.get("action", "deny"), f"{where}: action")

    reason = raw.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise PolicyError(f"{where}: 'reason' must be a string")

    source = raw.get("source", "curated")
    if not isinstance(source, str):
        raise PolicyError(f"{where}: 'source' must be a string")

    expires = raw.get("expires")
    if expires is not None:
        # tomllib may parse a TOML datetime to a datetime; accept both forms.
        if isinstance(expires, datetime):
            expires_dt = expires
            expires = expires.isoformat()
        elif isinstance(expires, str):
            try:
                expires_dt = datetime.fromisoformat(expires)
            except ValueError:
                raise PolicyError(f"{where}: 'expires' must be an RFC 3339 timestamp") from None
        else:
            raise PolicyError(f"{where}: 'expires' must be an RFC 3339 timestamp")
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
        if expires_dt <= datetime.now(timezone.utc):
            return None  # expired: silently dropped, not an error

    return Rule(
        ecosystem=ecosystem,
        name=name,
        namespace=namespace,
        versions=versions,
        action=action,
        reason=reason,
        source=source,
        expires=expires,
    )


_ALLOWED_TOP_LEVEL = {"schema", "defaults", "rules", "upstream", "osv"}
_ALLOWED_OSV_KEYS = {"malicious_packages"}


def parse_policy(data: bytes) -> Policy:
    """Parse + structurally validate the unified TOML policy. Raises PolicyError."""
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise PolicyError(f"policy.toml is not valid UTF-8: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise PolicyError(f"policy.toml is not valid TOML: {e}") from e

    unknown = set(doc) - _ALLOWED_TOP_LEVEL
    if unknown:
        keys = ", ".join(sorted(unknown))
        raise PolicyError(
            f"unknown top-level key {keys}; allowed: defaults, osv, rules, schema, upstream"
        )

    schema = doc.get("schema")
    if schema != 1:
        raise PolicyError(f"policy schema must be 1; got {schema!r}")

    defaults = _parse_defaults(doc.get("defaults"))

    upstream = doc.get("upstream")
    if upstream is None:
        min_age = DEFAULT_MIN_AGE
    elif not isinstance(upstream, dict):
        raise PolicyError("'upstream' must be a table")
    else:
        raw_min_age = upstream.get("min_age")
        if raw_min_age is None:
            raw_min_age = upstream.get("minimum_age")
        min_age = _validate_min_age(raw_min_age)

    osv = doc.get("osv")
    if osv is None:
        osv_malicious_packages = False
    elif not isinstance(osv, dict):
        raise PolicyError("'osv' must be a table")
    else:
        _reject_unknown_keys(osv, _ALLOWED_OSV_KEYS, "osv")
        raw_malicious = osv.get("malicious_packages", False)
        if not isinstance(raw_malicious, bool):
            raise PolicyError("osv.malicious_packages must be a boolean")
        osv_malicious_packages = raw_malicious

    raw_rules = doc.get("rules", [])
    if not isinstance(raw_rules, list):
        raise PolicyError("'rules' must be an array of tables")
    rules: list[Rule] = []
    for i, raw_rule in enumerate(raw_rules):
        rule = _parse_rule(raw_rule, i)
        if rule is not None:
            rules.append(rule)

    return Policy(
        schema=schema,
        defaults=defaults,
        rules=tuple(rules),
        min_age=min_age,
        osv_malicious_packages=osv_malicious_packages,
    )
