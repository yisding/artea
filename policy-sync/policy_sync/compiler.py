"""The compiler: turn a parsed Policy into the three native emit payloads,
resolving the simplified allow-wins precedence at COMPILE TIME, so each
enforcement engine only ever sees already-decided blocks.

Emit targets (unchanged engines):
- npm  -> the existing ``blocked:``/``upstream:`` YAML shape the Verdaccio filter
          plugin compiles (verdaccio-filter-artea/src/policy.ts). Hand-written
          YAML, a conservative js-yaml-readable subset (quoted strings, block
          lists), byte-stable so unchanged policy never bumps mtime/ETag.
- pypi -> a PEP 440 constraints text passed to devpi.apply_constraints.

Precedence (simplified, allow-wins): an ALLOW wins over a DENY at the granularity
given. A whole-package allow un-blocks that package entirely (drops its denies).
A single exact-version allow un-blocks that exact version. Allow rules support
ONLY whole-package or a single exact-version; anything richer is rejected.

Atomicity: the whole policy is validated and all three artifacts are produced
before the caller writes anything, so a broken policy never touches enforcement
(last-known-good is preserved by the caller).
"""

from dataclasses import dataclass, field

from .adapters import NPM, PYPI, NpmAdapter, PypiAdapter
from .policy_model import Action, Policy, PolicyError, Rule


@dataclass(frozen=True)
class CompiledArtifacts:
    npm_yaml: str  # full npm-rules.yaml text (upstream: + blocked:)
    pypi_constraints: str  # PEP 440 constraints text (may be "")
    upstream_yaml: str  # upstream-policy.yaml text the CompositePolicyLoader reads
    min_age: str  # top-level upstream.min_age (ISO 8601)


@dataclass
class _EcosystemRules:
    """Normalized, adapter-validated rules for one ecosystem."""

    namespace_denies: set[str] = field(default_factory=set)
    # name -> reason (whole-package deny)
    whole_denies: dict[str, str | None] = field(default_factory=dict)
    # name -> list of (range, reason)
    range_denies: dict[str, list[tuple[str, str | None]]] = field(default_factory=dict)
    whole_allows: set[str] = field(default_factory=set)
    # name -> set of exact versions allowed
    exact_allows: dict[str, set[str]] = field(default_factory=dict)
    # name -> set of allowed namespaces (whole-namespace allow)
    namespace_allows: set[str] = field(default_factory=set)


def _classify(
    rules: tuple[Rule, ...], ecosystem: str, adapter: NpmAdapter | PypiAdapter
) -> _EcosystemRules:
    """Normalize + validate every rule for one ecosystem into deny/allow buckets."""
    out = _EcosystemRules()
    for i, rule in enumerate(rules):
        if rule.ecosystem != ecosystem:
            continue
        where = f"rule {i}"
        if rule.namespace is not None:
            if not adapter.supports_namespace():
                raise PolicyError(f"{where}: ecosystem '{ecosystem}' does not support namespaces")
            ns = adapter.normalize_namespace(rule.namespace)
            if rule.action is Action.DENY:
                out.namespace_denies.add(ns)
            else:
                out.namespace_allows.add(ns)
            continue

        name = adapter.normalize_name(rule.name)  # type: ignore[arg-type]

        if rule.versions is not None:
            try:
                adapter.validate_range(rule.versions)
            except PolicyError as e:
                raise PolicyError(
                    f"{where}: {ecosystem} package '{name}' has {e}"
                ) from None

        if rule.action is Action.ALLOW:
            if rule.versions is None:
                out.whole_allows.add(name)
            elif adapter.is_exact(rule.versions):
                out.exact_allows.setdefault(name, set()).add(adapter.exact_value(rule.versions))
            else:
                raise PolicyError(
                    f"{where}: allow rules support only a whole package or a single "
                    f"exact version, not a range '{rule.versions}'"
                )
            continue

        # DENY
        if rule.versions is None:
            out.whole_denies[name] = rule.reason
        else:
            out.range_denies.setdefault(name, []).append((rule.versions, rule.reason))
    return out


# ----------------------------------------------------------------- npm emission


# short YAML double-quoted escapes for the common control chars.
_YAML_SHORT_ESCAPES = {
    "\t": "\\t",
    "\n": "\\n",
    "\r": "\\r",
}


def _npm_quote(s: str) -> str:
    """Emit ``s`` as a correct YAML double-quoted scalar js-yaml round-trips back
    to the exact original string.

    The npm artifact is hand-written YAML read by js-yaml. The previous version
    escaped only ``\\`` and ``"``; a newline or control char in a name/range/reason
    then either silently ALTERED the value (js-yaml folds a raw ``"a\\nb"`` to
    ``"a b"`` -> blocks the WRONG package) or THREW on a NUL/ESC -> the filter
    rejects the file -> ALL npm fails closed. Escaping (not rejecting) keeps the
    right package blocked while still freezing reasons that legitimately contain
    punctuation. Control-free inputs are emitted byte-identically (no churn).
    """
    out = ['"']
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch in _YAML_SHORT_ESCAPES:
            out.append(_YAML_SHORT_ESCAPES[ch])
        elif ch < "\x20" or ch == "\x7f":
            # other C0 controls + DEL -> \xNN (two lowercase hex)
            out.append(f"\\x{ord(ch):02x}")
        elif ord(ch) in (0x2028, 0x2029):
            # YAML/JS line/paragraph separators js-yaml treats as line breaks
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _resolve_npm(eco: _EcosystemRules, adapter: NpmAdapter) -> tuple[list[str], list, dict]:
    """Apply allow-wins and return (scopes, packages, ranges) ready to emit.

    packages: sorted list of bare whole-package deny names.
    ranges: name -> sorted list of (range, reason) the filter ORs together.
    """
    scopes = set(eco.namespace_denies)
    # whole-namespace allow drops a matching scope deny entirely.
    scopes -= eco.namespace_allows

    whole = dict(eco.whole_denies)
    ranges: dict[str, list[tuple[str, str | None]]] = {
        n: list(rs) for n, rs in eco.range_denies.items()
    }

    # whole-package allow: drop every deny for that exact package.
    for name in eco.whole_allows:
        had_pkg_deny = name in whole or name in ranges
        whole.pop(name, None)
        ranges.pop(name, None)
        if not had_pkg_deny:
            # the package may still be covered by a namespace deny; the block-list
            # filter cannot express "scope minus one name".
            if _covered_by_scope(name, scopes):
                raise PolicyError(
                    "npm: cannot allow a single package out of a namespace deny"
                )
            # otherwise a no-op (already allowed by absence)

    # exact-version allow: carve the point out of a whole-package deny via the
    # semver complement. Against a range deny it is range-vs-range -> reject.
    for name, versions in eco.exact_allows.items():
        if name in ranges:
            raise PolicyError(
                f"npm: an allow against a version-range deny (range carving) "
                f"is not supported yet (package '{name}')"
            )
        if name in whole:
            reason = whole.pop(name)
            for v in sorted(versions):
                complement = f"<{v} || >{v}"
                try:
                    adapter.validate_range(complement)
                except PolicyError:
                    raise PolicyError(
                        f"npm: cannot express the allow carve-out for "
                        f"'{name}=={v}' as a semver range"
                    ) from None
                ranges.setdefault(name, []).append((complement, reason))
        # exact allow against no deny on this name -> no-op

    packages = sorted(whole)
    return sorted(scopes), packages, ranges


def _covered_by_scope(name: str, scopes: set[str]) -> bool:
    if not name.startswith("@") or "/" not in name:
        return False
    return name[: name.index("/")] in scopes


def _emit_npm(
    eco: _EcosystemRules, adapter: NpmAdapter, min_age: str, default_action: Action
) -> str:
    if default_action is Action.DENY:
        raise PolicyError(
            "npm: default-deny is not supported (the Verdaccio filter is a "
            "block-list); use deny rules instead"
        )

    scopes, packages, ranges = _resolve_npm(eco, adapter)

    lines: list[str] = ["upstream:", f"  min_age: {_npm_quote(min_age)}"]

    has_blocked = bool(scopes or packages or ranges)
    if not has_blocked:
        return "\n".join(lines) + "\n"

    lines.append("blocked:")
    if scopes:
        lines.append("  scopes:")
        for scope in scopes:
            lines.append(f"    - {_npm_quote(scope)}")
    if packages or ranges:
        lines.append("  packages:")
        for name in packages:
            lines.append(f"    - {_npm_quote(name)}")
        for name in sorted(ranges):
            for rng, reason in sorted(ranges[name], key=lambda t: t[0]):
                # round-trip guard: never emit a range the filter would reject.
                adapter.validate_range(rng)
                lines.append(f"    - name: {_npm_quote(name)}")
                lines.append(f"      versions: {_npm_quote(rng)}")
                if reason:
                    lines.append(f"      reason: {_npm_quote(reason)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- pypi emission


def _validate_pypi_line(adapter: PypiAdapter, line: str) -> None:
    """Round-trip guard for a single emitted constraints line: the specifier
    portion (everything from the first comparator char) must be a valid PEP 440
    specifier the engine accepts. Bare passthrough names and '*' carry no
    comparator and are exempt.
    """
    if not any(op in line for op in ("==", "!=", "<", ">", "~=")):
        return
    for i, ch in enumerate(line):
        if ch in "=!<>~":
            adapter.validate_range(line[i:])
            return


def _emit_pypi_ranges(
    adapter: PypiAdapter, ranges: dict[str, list[tuple[str, str | None]]]
) -> list[str]:
    """Range denies -> devpi reads a constraint as an ALLOW set, so emit the
    COMPLEMENT of each deny range. Multiple denies for one package combine into a
    SINGLE comma-joined specifier (devpi rejects a repeated project name), the
    intersection of each deny's complement. Each emitted line is validated here.
    """
    out: list[str] = []
    for name in sorted(ranges):
        specs = [
            adapter.complement(rng)
            for rng, _reason in sorted(ranges[name], key=lambda t: t[0])
        ]
        combined = ",".join(specs)
        # round-trip guard: the combined complement must be one valid specifier set.
        try:
            adapter.validate_range(combined)
        except PolicyError:
            raise PolicyError(
                f"pypi: the denies for '{name}' combine into "
                f"{combined!r}, which is not a single valid PEP 440 "
                f"specifier set devpi can accept"
            ) from None
        # the combined specifier can be syntactically valid yet describe an EMPTY
        # allow-set (e.g. deny >=2 + deny <5 -> complements >=5,<2). That would
        # silently block the whole package, which the round-trip syntax guard
        # above cannot catch. Reject it with an actionable message.
        if adapter.complement_set_is_empty(specs):
            raise PolicyError(
                f"pypi: the denies for '{name}' combine into {combined!r}, an "
                f"empty allow-set that would silently block the whole package; "
                f"use a whole-package deny instead"
            )
        line = f"{name}{combined}"
        _validate_pypi_line(adapter, line)
        out.append(line)
    return out


def _emit_pypi_wholes(
    adapter: PypiAdapter, whole: dict[str, str | None], default_deny: bool
) -> list[str]:
    """Whole-package denies -> '==0' kill sentinel under default-allow; under
    default-deny the package is already blocked by the trailing '*'.
    """
    if default_deny:
        return []
    out: list[str] = []
    for name in sorted(whole):
        line = f"{name}==0"
        _validate_pypi_line(adapter, line)
        out.append(line)
    return out


def _emit_pypi_allows(
    adapter: PypiAdapter,
    exact_passthrough: dict[str, str],
    allow_passthrough: set[str],
) -> list[str]:
    """Allow passthroughs, in emit order: exact-version allows (constrain to ==v,
    the only version that passes — whether carving out of a whole-package deny or
    whitelisting under default-deny), then whole-package allows under default-deny
    (the bare name listed above '*').
    """
    out: list[str] = []
    for name in sorted(exact_passthrough):
        line = f"{name}=={exact_passthrough[name]}"
        _validate_pypi_line(adapter, line)
        out.append(line)
    # whole-package allow under default-deny -> list the bare name above '*'.
    for name in sorted(allow_passthrough):
        _validate_pypi_line(adapter, name)
        out.append(name)
    return out


def _emit_pypi(eco: _EcosystemRules, adapter: PypiAdapter, default_action: Action) -> str:
    default_deny = default_action is Action.DENY

    whole = dict(eco.whole_denies)
    ranges: dict[str, list[tuple[str, str | None]]] = {
        n: list(rs) for n, rs in eco.range_denies.items()
    }

    # whole-package allow: drop denies for that package; under default-deny the
    # bare name must be listed above '*' so it still passes.
    allow_passthrough: set[str] = set()
    for name in eco.whole_allows:
        whole.pop(name, None)
        ranges.pop(name, None)
        if default_deny:
            allow_passthrough.add(name)

    # exact-version allow: against a whole-package deny -> constrain to ==v (which
    # un-blocks exactly v). Against a range deny -> range-vs-range, reject. Under
    # default-deny an exact allow is the PRIMARY allow-list mechanism, so it must
    # pass that version even with no deny on the name; under default-allow with no
    # deny it is a no-op (the version is already allowed).
    #
    # devpi's constrained index accepts a single specifier per project and a
    # disjunction of exact versions ("==a OR ==b") is not expressible as one PEP
    # 440 specifier set, so reject more than one exact-version allow for a package
    # rather than emit a duplicate project line devpi's parse_constraints rejects
    # (which would compile clean but fail when applied to the index).
    exact_passthrough: dict[str, str] = {}
    for name, versions in eco.exact_allows.items():
        if name in eco.whole_allows:
            # a whole-package allow already passes every version (including this
            # one), so the exact allow is redundant. Under default-deny the name
            # is already listed as a bare passthrough; emitting '==v' too would be
            # a duplicate project line devpi rejects.
            continue
        if name in ranges:
            raise PolicyError(
                f"pypi: an allow against a version-range deny (range carving) "
                f"is not supported yet (package '{name}')"
            )
        emit_exact = name in whole or default_deny
        whole.pop(name, None)
        if not emit_exact:
            continue  # default-allow + no deny -> already allowed, no-op
        if len(versions) > 1:
            joined = ", ".join(f"=={v}" for v in sorted(versions))
            raise PolicyError(
                f"pypi: package '{name}' has multiple exact-version allows "
                f"({joined}); devpi's constrained index accepts only one "
                f"specifier per project and a disjunction of exact versions is "
                f"not a valid PEP 440 specifier set. Allow a single exact "
                f"version, or allow the whole package."
            )
        (exact_passthrough[name],) = versions

    lines: list[str] = []
    lines += _emit_pypi_ranges(adapter, ranges)
    lines += _emit_pypi_wholes(adapter, whole, default_deny)
    lines += _emit_pypi_allows(adapter, exact_passthrough, allow_passthrough)
    if default_deny:
        lines.append("*")

    return "\n".join(lines) + ("\n" if lines else "")


# --------------------------------------------------------------------- compile


def compile_policy(policy: Policy) -> CompiledArtifacts:
    """Compile a parsed Policy into the three native artifacts. Raises PolicyError.

    Validates the whole policy and produces all artifacts before returning, so a
    partial/inconsistent emit is never handed to the caller.
    """
    npm_adapter = NPM
    pypi_adapter = PYPI

    npm_eco = _classify(policy.rules, "npm", npm_adapter)
    pypi_eco = _classify(policy.rules, "pypi", pypi_adapter)

    npm_yaml = _emit_npm(
        npm_eco, npm_adapter, policy.min_age, policy.defaults.for_ecosystem("npm")
    )
    pypi_constraints = _emit_pypi(
        pypi_eco, pypi_adapter, policy.defaults.for_ecosystem("pypi")
    )

    # The Verdaccio filter is always wired with the CompositePolicyLoader, which
    # takes min_age SOLELY from upstream-policy.yaml (it discards the npm policy's
    # own minAgeMs). Emit that artifact too so the unified path reaches the npm
    # quarantine gate and a fresh unified-only deployment does not fail closed.
    upstream_yaml = f"upstream:\n  min_age: {_npm_quote(policy.min_age)}\n"

    return CompiledArtifacts(
        npm_yaml=npm_yaml,
        pypi_constraints=pypi_constraints,
        upstream_yaml=upstream_yaml,
        min_age=policy.min_age,
    )
