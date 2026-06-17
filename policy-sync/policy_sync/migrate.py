"""Best-effort migration generator: read the three legacy policy files
(``npm-rules.yaml`` / ``pypi-constraints.txt`` / ``upstream-policy.yaml``) and
emit an equivalent unified ``policy.toml``.

Run as a module::

    python -m policy_sync.migrate [policy_dir] [--out PATH]

``policy_dir`` defaults to ``policy/``. The generated TOML is written to stdout
(or ``--out``); warnings for anything the unified schema cannot express
faithfully go to stderr. Stdlib-only: the legacy YAML is read with a tiny
hand-rolled parser for the known shapes (no PyYAML), mirroring the line-based
extraction the daemon already uses (``sync.py:_extract_min_upstream_age``).

The mapping is intentionally conservative — when a legacy construct cannot be
round-tripped cleanly through the unified schema the migrator skips it and warns
rather than emit something that changes meaning. Always review the output before
committing it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .adapters import _PEP440_COMPLEMENT_OP, _PEP440_SINGLE_CMP_RE, NpmAdapter, PypiAdapter

NPM_RULES_FILE = "npm-rules.yaml"
PYPI_CONSTRAINTS_FILE = "pypi-constraints.txt"
UPSTREAM_POLICY_FILE = "upstream-policy.yaml"

_npm = NpmAdapter()
_pypi = PypiAdapter()


def _warn(msg: str) -> None:
    print(f"policy-sync migrate: warning: {msg}", file=sys.stderr)


# --------------------------------------------------------------------- TOML emit


def _toml_str(s: str) -> str:
    """Emit ``s`` as a TOML basic string (double-quoted with escapes)."""
    out = ['"']
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch < "\x20" or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


# ----------------------------------------------------------------- npm parsing


def _parse_npm_rules(text: str) -> tuple[list[str], list[dict]]:
    """Parse the legacy ``npm-rules.yaml`` subset.

    Returns ``(scopes, packages)`` where each package is a dict with a ``name``
    and optional ``versions`` / ``reason``. The legacy schema is a tiny YAML
    subset (``blocked:`` -> ``scopes:`` list and ``packages:`` list of bare
    strings or ``{name, versions, reason}`` mappings), so a structural
    line-based reader suffices (no PyYAML, stdlib-only).
    """
    scopes: list[str] = []
    packages: list[dict] = []

    section: str | None = None  # None | "scopes" | "packages"
    in_blocked = False
    current: dict | None = None

    def _flush() -> None:
        nonlocal current
        if current is not None and current.get("name"):
            packages.append(current)
        current = None

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        if indent == 0:
            _flush()
            in_blocked = stripped == "blocked:"
            section = None
            continue
        if not in_blocked:
            continue

        if indent == 2 and stripped in ("scopes:", "packages:"):
            _flush()
            section = stripped[:-1]
            continue

        if section == "scopes" and stripped.startswith("- "):
            scopes.append(_unquote(stripped[2:].strip()))
            continue

        if section == "packages":
            if stripped.startswith("- "):
                _flush()
                rest = stripped[2:].strip()
                if rest.startswith("name:"):
                    current = {"name": _unquote(rest[len("name:"):].strip())}
                elif ":" in rest and not rest.startswith(("'", '"')):
                    # an inline "key: value" that is not name: -> start a mapping
                    current = {}
                    _apply_kv(current, rest)
                else:
                    # a bare string -> whole-package block
                    packages.append({"name": _unquote(rest)})
                    current = None
            elif current is not None and ":" in stripped:
                _apply_kv(current, stripped)
    _flush()
    return scopes, packages


def _apply_kv(target: dict, kv: str) -> None:
    key, sep, value = kv.partition(":")
    if not sep:
        return
    key = key.strip()
    value = _unquote(value.strip())
    if key in ("name", "versions", "reason"):
        target[key] = value


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


# ---------------------------------------------------------------- pypi parsing


def _parse_pypi_constraints(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _invert_pypi_line(line: str) -> tuple[dict | None, str | None]:
    """Map one legacy constraint line to a unified rule (best-effort).

    A constraints line is an ALLOW-list ("only these versions are visible"); the
    unified schema is deny-primary, so the faithful equivalent of an allow
    constraint ``name<2`` is a deny of the complement ``name >= 2``. Returns
    ``(rule_or_None, warning_or_None)``: a rule dict, or None plus a warning
    string for lines the schema cannot round-trip cleanly.
    """
    if line == "*":
        return None, (
            "ignored the '*' default-deny line; the unified schema cannot express "
            "a per-line default-deny — set [defaults.ecosystems.pypi] action = "
            '"deny" manually if you want a default-deny baseline'
        )

    # split name from specifier: name is the leading run of name chars.
    i = 0
    while i < len(line) and (line[i].isalnum() or line[i] in "-_."):
        i += 1
    name = line[:i].strip()
    spec = line[i:].strip()
    if not name:
        return None, f"could not parse package name from constraint line {line!r}; skipped"

    try:
        norm = _pypi.normalize_name(name)
    except Exception:  # PolicyError, etc.
        return None, f"invalid pypi package name in line {line!r}; skipped"

    if not spec:
        return None, f"constraint line {line!r} has no specifier; skipped"

    # kill sentinel "==0" -> whole-package deny.
    if spec.replace(" ", "") == "==0":
        return (
            {"ecosystem": "pypi", "name": norm, "action": "deny",
             "reason": "migrated from legacy ==0 kill sentinel"},
            f"line {line!r} was a '==0' kill sentinel; migrated to a whole-package deny",
        )

    # a single complementable comparator -> deny the complement of the allow.
    m = _PEP440_SINGLE_CMP_RE.match(spec)
    if m and _is_invertible_allow(spec):
        op, version = m.group(1), m.group(2)
        deny_range = f"{_INVERT_ALLOW_OP[op]}{version}"
        return (
            {"ecosystem": "pypi", "name": norm, "versions": deny_range, "action": "deny"},
            None,
        )

    return None, (
        f"could not invert constraint line {line!r} into a single deny rule "
        f"(compound sets, ~=, ===, and ==X.* allow-lists have no single-deny "
        f"equivalent); skipped — express the intended block as explicit deny rules"
    )


# the constraints file holds an ALLOW spec; the equivalent deny is a deny of the
# COMPLEMENT of that allow. complement(deny) -> allow is _PEP440_COMPLEMENT_OP;
# inverting it gives allow-op -> deny-op.
_INVERT_ALLOW_OP = {allow: deny for deny, allow in _PEP440_COMPLEMENT_OP.items()}


def _is_invertible_allow(spec: str) -> bool:
    """A single ``<``/``<=``/``>``/``>=`` comparator is cleanly invertible into a
    single deny range; ``==`` allows only one version (its complement is ``!=``,
    not a single deny range the compiler can re-emit), so we do not auto-invert.
    """
    op = spec.lstrip()[:2]
    return op[0] in "<>" if op else False


# -------------------------------------------------------------- upstream parsing


def _parse_upstream_min_age(text: str) -> str | None:
    in_upstream = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0:
            in_upstream = stripped == "upstream:"
            continue
        if in_upstream and indent > 0:
            key, sep, value = stripped.partition(":")
            if sep and key.strip() in {"min_age", "minimum_age", "min_upstream_age"}:
                return _unquote(value.strip())
    return None


# ------------------------------------------------------------------- generation


def generate_policy_toml(policy_dir: Path) -> str:
    """Read the three legacy files in ``policy_dir`` and return policy.toml text.

    Missing legacy files are treated as empty (their absence simply contributes
    no rules). Warnings for inexpressible constructs are printed to stderr.
    """
    rules: list[dict] = []

    npm_path = policy_dir / NPM_RULES_FILE
    if npm_path.is_file():
        scopes, packages = _parse_npm_rules(npm_path.read_text(encoding="utf-8"))
        for scope in scopes:
            try:
                ns = _npm.normalize_namespace(scope)
            except Exception:
                _warn(f"invalid npm scope {scope!r}; skipped")
                continue
            rules.append({"ecosystem": "npm", "namespace": ns, "action": "deny"})
        for pkg in packages:
            rule: dict = {"ecosystem": "npm", "name": pkg["name"], "action": "deny"}
            if pkg.get("versions"):
                rule["versions"] = pkg["versions"]
            if pkg.get("reason"):
                rule["reason"] = pkg["reason"]
            rules.append(rule)
    else:
        _warn(f"{NPM_RULES_FILE} not found in {policy_dir}; no npm rules migrated")

    pypi_path = policy_dir / PYPI_CONSTRAINTS_FILE
    if pypi_path.is_file():
        for line in _parse_pypi_constraints(pypi_path.read_text(encoding="utf-8")):
            rule, warning = _invert_pypi_line(line)
            if warning:
                _warn(warning)
            if rule is not None:
                rules.append(rule)
    else:
        _warn(f"{PYPI_CONSTRAINTS_FILE} not found in {policy_dir}; no pypi rules migrated")

    min_age: str | None = None
    upstream_path = policy_dir / UPSTREAM_POLICY_FILE
    if upstream_path.is_file():
        min_age = _parse_upstream_min_age(upstream_path.read_text(encoding="utf-8"))
    else:
        _warn(f"{UPSTREAM_POLICY_FILE} not found in {policy_dir}; using default min_age")

    return _render(rules, min_age)


def _render(rules: list[dict], min_age: str | None) -> str:
    out: list[str] = [
        "# Generated by `python -m policy_sync.migrate` from the legacy policy",
        "# files. Best-effort conversion — review before committing. See",
        "# docs/policy-schema.md for the unified schema.",
        "schema = 1",
        "",
        "[defaults]",
        'action = "allow"',
    ]
    if min_age is not None and min_age != "P0D":
        out += ["", "[upstream]", f"min_age = {_toml_str(min_age)}"]

    for rule in rules:
        out += ["", "[[rules]]"]
        out.append(f'ecosystem = {_toml_str(rule["ecosystem"])}')
        if "namespace" in rule:
            out.append(f'namespace = {_toml_str(rule["namespace"])}')
        if "name" in rule:
            out.append(f'name = {_toml_str(rule["name"])}')
        if "versions" in rule:
            out.append(f'versions = {_toml_str(rule["versions"])}')
        out.append(f'action = {_toml_str(rule["action"])}')
        if "reason" in rule:
            out.append(f'reason = {_toml_str(rule["reason"])}')

    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    policy_dir = Path("policy")
    out_path: Path | None = None

    args = list(argv)
    i = 0
    positional: list[str] = []
    while i < len(args):
        arg = args[i]
        if arg in ("-o", "--out"):
            if i + 1 >= len(args):
                print("migrate: --out requires a path", file=sys.stderr)
                return 2
            out_path = Path(args[i + 1])
            i += 2
            continue
        if arg in ("-h", "--help"):
            print(__doc__)
            return 0
        positional.append(arg)
        i += 1

    if positional:
        policy_dir = Path(positional[0])

    if not policy_dir.is_dir():
        print(f"migrate: {policy_dir} is not a directory", file=sys.stderr)
        return 2

    toml_text = generate_policy_toml(policy_dir)

    if out_path is not None:
        out_path.write_text(toml_text, encoding="utf-8")
        print(f"policy-sync migrate: wrote {out_path}", file=sys.stderr)
    else:
        sys.stdout.write(toml_text)

    print(
        "policy-sync migrate: review the generated policy.toml before committing.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
