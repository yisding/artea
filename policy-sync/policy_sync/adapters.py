"""Per-ecosystem adapters: name normalization, namespace support, native-range
validation, and the two precedence primitives the simplified allow-wins model
needs (is_exact / exact_value).

Stdlib-only (no semver / packaging dependency). The npm and pypi range
validators are deliberately *conservative subsets* of the dialects the
enforcement engines accept:

- npm: the source of truth is the Verdaccio filter's
  ``semver.validRange(r, {includePrerelease: true, loose: true})``. This
  validator accepts only the comparator grammar the filter also accepts, so a
  range that passes here is always one the filter will accept. When in doubt we
  reject at compile time rather than emit a range the filter might reject — that
  protects the npm fail-closed invariant (emitting a bad range would tear down
  the whole npm policy at the filter).
- pypi: a focused PEP 440 specifier-set validator. Emitted lines are PATCHed
  verbatim onto the devpi ``root/constrained`` index.

No general range algebra lives here (explicit scope steer). The only
"subtraction" is the narrow exact-point npm complement built in the compiler
from ``exact_value``.
"""

import re

from .policy_model import PolicyError


# --------------------------------------------------------------------------- npm

# a single semver version: 1, 1.2, 1.2.3, with optional -prerelease / +build and
# a TRAILING x/X/* wildcard. (loose mode lets the filter accept 1 and 1.2.)
#
# Crucially a wildcard may only appear in a trailing position: once a wildcard
# occupies a segment, every later segment must also be a wildcard. semver's
# validRange ACCEPTS x, 1.x, 1.2.x, 1.x.x but REJECTS a concrete segment after a
# wildcard (1.x.3, x.2.3, X.2, 1.X.3, 0.x.0). The old all-positions _NPM_NUM
# wrongly accepted those, so the compiler could emit a range the filter rejects
# -> the filter throws -> {ok:false} -> ALL npm fails closed, and the round-trip
# guard (which shares this validator) could not catch it. By enforcing
# trailing-only wildcards here, anything this subset accepts is genuinely
# accepted by semver.validRange, so the round-trip guard is trustworthy.
_NPM_NUMSEG = r"(?:0|[1-9]\d*)"
_NPM_WILD = r"(?:[xX*])"
_NPM_PRE = r"(?:-[0-9A-Za-z.-]+)?"
_NPM_BUILD = r"(?:\+[0-9A-Za-z.-]+)?"
# Up to three dotted segments where a WILD forbids any LATER numeric segment but
# a later .WILD is fine (semver accepts x, 1.x, 1.2.x, 1.x.x but rejects 1.x.3,
# x.2.3, X.2, 1.X.3, 0.x.0). We enumerate the exact accepted shapes:
#   WILD                       (x / * / X)
#   NUM                        (1)
#   NUM.WILD                   (1.x)        NUM.NUM (1.2)
#   NUM.WILD.WILD (1.x.x)  NUM.NUM.WILD (1.2.x)  NUM.NUM.NUM (1.2.3)
# i.e. once a WILD appears every following segment must also be a WILD.
#
# The PRERELEASE suffix (-alpha, -rc.1, ...) may attach ONLY to a FULL three
# segment numeric version. semver's loose mode attaches a prerelease to a
# complete major.minor.patch only; it REJECTS a prerelease on a partial version
# ('1-alpha', '1.2-rc.1' -> validRange returns null), which would make the filter
# throw and fail-close ALL npm. A BUILD metadata suffix (+build) alone IS
# accepted by semver even on a partial version, so the partial branches keep the
# build suffix but drop the prerelease one.
_NPM_VERSION = (
    rf"v?(?:"
    rf"{_NPM_WILD}"
    rf"|{_NPM_NUMSEG}\.{_NPM_WILD}(?:\.{_NPM_WILD})?"
    rf"|{_NPM_NUMSEG}\.{_NPM_NUMSEG}\.{_NPM_WILD}"
    rf"|{_NPM_NUMSEG}\.{_NPM_NUMSEG}\.{_NPM_NUMSEG}{_NPM_PRE}{_NPM_BUILD}"
    rf"|{_NPM_NUMSEG}(?:\.{_NPM_NUMSEG})?{_NPM_BUILD}"
    rf")"
)

# a single comparator: optional operator (or ^ / ~) + version, or a bare * , or a
# hyphen range "a - b".
_NPM_OP = r"(?:<=|>=|<|>|=|\^|~)?"
_NPM_SIMPLE = rf"{_NPM_OP}\s*{_NPM_VERSION}"
_NPM_HYPHEN = rf"{_NPM_VERSION}\s+-\s+{_NPM_VERSION}"
_NPM_STAR = r"[xX*]"
_NPM_COMPARATOR = rf"(?:{_NPM_HYPHEN}|{_NPM_SIMPLE}|{_NPM_STAR})"
# an AND set: space-separated comparators. An OR range: '||'-joined AND sets.
_NPM_AND = rf"{_NPM_COMPARATOR}(?:\s+{_NPM_COMPARATOR})*"
_NPM_RANGE_RE = re.compile(rf"^\s*{_NPM_AND}(?:\s*\|\|\s*{_NPM_AND})*\s*$")

# an exact pinned version: a bare X.Y.Z (digits only, no operator, no wildcard).
_NPM_EXACT_NUM = r"(?:0|[1-9]\d*)"
_NPM_EXACT_RE = re.compile(
    rf"^\s*=?\s*(v?{_NPM_EXACT_NUM}\.{_NPM_EXACT_NUM}\.{_NPM_EXACT_NUM}{_NPM_PRE}{_NPM_BUILD})\s*$"
)


class NpmAdapter:
    ecosystem = "npm"
    osv_ecosystem = "npm"  # OSV.dev ecosystem casing for querybatch (osv.py)

    def normalize_name(self, name: str) -> str:
        return name.strip().lower()

    def supports_namespace(self) -> bool:
        return True

    def normalize_namespace(self, ns: str) -> str:
        ns = ns.strip()
        return ns if ns.startswith("@") else f"@{ns}"

    def validate_range(self, expr: str) -> None:
        if not _NPM_RANGE_RE.match(expr):
            raise PolicyError(f"invalid semver range {expr!r}")

    def is_exact(self, expr: str) -> bool:
        return bool(_NPM_EXACT_RE.match(expr))

    def exact_value(self, expr: str) -> str:
        m = _NPM_EXACT_RE.match(expr)
        if not m:
            raise PolicyError(f"{expr!r} is not an exact npm version")
        v = m.group(1)
        return v[1:] if v.startswith("v") else v

    def exact_allows(self, expr: str, version: str) -> bool:
        """Whether the exact pin ``expr`` matches ``version``. npm versions are
        canonical ``X.Y.Z``, so equality is a string match on the pin."""
        return self.exact_value(expr) == version


# -------------------------------------------------------------------------- pypi

# the PEP 503 normalized name shape devpi's parse_requirement / normalize_name
# accepts. normalize_name collapses runs of -_. to a single - and lowercases, so
# the normalized form must match this; anything else (a space, '#', '/', a
# newline, an empty string) would emit a constraint line devpi's parse_constraints
# rejects -> the whole PATCH 400s -> ALL pypi denies fail to freeze.
_PYPI_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# PEP 440 version token core (subset sufficient for specifiers): epoch, release,
# pre/post/dev. Defined once and shared by every pattern below so the three
# version matchers cannot drift out of sync.
_PEP440_CORE = (
    r"(?:\d+!)?\d+(?:\.\d+)*"          # [epoch!]release
    r"(?:(?:a|b|c|rc|alpha|beta|pre|preview)\.?\d*)?"  # pre-release
    r"(?:(?:-|\.|_)?(?:post|rev|r)\.?\d*)?"            # post-release
    r"(?:(?:-|\.|_)?dev\.?\d*)?"                        # dev-release
)
# version token with a trailing .* for == / != prefix matching.
_PEP440_VERSION = _PEP440_CORE + r"(?:\.\*)?"
_PEP440_SPEC = rf"(?:===|==|!=|<=|>=|~=|<|>)\s*{_PEP440_VERSION}"
_PEP440_SET_RE = re.compile(rf"^\s*{_PEP440_SPEC}(?:\s*,\s*{_PEP440_SPEC})*\s*$")
# a SINGLE complementable comparator: one of < <= > >= == against a plain
# version (no wildcard, no comma-set). Captures (op, version).
_PEP440_SINGLE_CMP_RE = re.compile(rf"^\s*(<=|>=|==|<|>)\s*({_PEP440_CORE})\s*$")
_PEP440_COMPLEMENT_OP = {"<": ">=", "<=": ">", ">": "<=", ">=": "<", "==": "!="}
# a plain dotted-numeric release with no epoch/pre/post/dev/local suffix — the
# only shape whose ordering a bare int tuple can safely compare.
_PEP440_PLAIN_RELEASE_RE = re.compile(r"\d+(?:\.\d+)*")
# a single "==X" with no wildcard (an exact pin used by the allow escape hatch).
_PEP440_EXACT_RE = re.compile(rf"^\s*==\s*({_PEP440_CORE})\s*$")


class PypiAdapter:
    ecosystem = "pypi"
    osv_ecosystem = "PyPI"  # OSV.dev ecosystem casing for querybatch (osv.py)

    def normalize_name(self, name: str) -> str:
        normalized = re.sub(r"[-_.]+", "-", name.strip()).lower()
        if not _PYPI_NAME_RE.match(normalized):
            raise PolicyError(f"invalid pypi package name {name!r}")
        return normalized

    def supports_namespace(self) -> bool:
        return False

    def normalize_namespace(self, ns: str) -> str:  # pragma: no cover - never called
        raise PolicyError("pypi does not support namespaces")

    def validate_range(self, expr: str) -> None:
        if not _PEP440_SET_RE.match(expr):
            raise PolicyError(f"invalid PEP 440 specifier {expr!r}")

    def is_exact(self, expr: str) -> bool:
        return bool(_PEP440_EXACT_RE.match(expr))

    def exact_value(self, expr: str) -> str:
        m = _PEP440_EXACT_RE.match(expr)
        if not m:
            raise PolicyError(f"{expr!r} is not an exact pypi version")
        return m.group(1)

    def exact_allows(self, expr: str, version: str) -> bool:
        """Whether the exact pin ``expr`` (an ``==X``) matches ``version`` under
        PEP 440 release-equality, so ``==1.0`` allows an upstream ``1.0.0``.

        Conservative: a version carrying an epoch/pre/post/dev/local segment —
        where a bare release tuple cannot decide equality — falls back to exact
        string equality, so this never reports a false match.
        """
        pinned = self.exact_value(expr)
        if pinned == version:
            return True
        pinned_rel = self._release_tuple(pinned)
        version_rel = self._release_tuple(version)
        if pinned_rel is None or version_rel is None:
            return False
        width = max(len(pinned_rel), len(version_rel))
        return (
            pinned_rel + (0,) * (width - len(pinned_rel))
            == version_rel + (0,) * (width - len(version_rel))
        )

    def complement(self, expr: str) -> str:
        """Return the PEP 440 specifier that ALLOWS everything the deny range
        ``expr`` blocks the complement of. devpi treats a constraint as an
        allow-list, so a deny range must be emitted as its complement.

        Only a SINGLE comparator (< <= > >= ==) is complementable into one
        specifier; compound sets, ~=, ===, and ==X.* wildcards are rejected
        because their complement is not a single PEP 440 specifier.
        """
        m = _PEP440_SINGLE_CMP_RE.match(expr)
        if not m:
            raise PolicyError(
                f"deny range {expr!r} cannot be inverted into a single devpi "
                f"allow-constraint; only a single <, <=, >, >=, or == comparator "
                f"against a plain version is supported"
            )
        op, version = m.group(1), m.group(2)
        return f"{_PEP440_COMPLEMENT_OP[op]}{version}"

    def _release_tuple(self, version: str) -> tuple[int, ...] | None:
        """Return the dotted-numeric release of a plain PEP 440 version as an int
        tuple, or None if it carries a pre/post/dev/epoch suffix (not safely
        comparable with a bare tuple). Used only for the contradiction check below.
        """
        # strip an epoch (we can't compare across epochs with a plain tuple)
        if "!" in version:
            return None
        # a pre/post/dev/local suffix makes simple tuple ordering unsafe; bail out.
        if not _PEP440_PLAIN_RELEASE_RE.fullmatch(version):
            return None
        return tuple(int(p) for p in version.split("."))

    def complement_set_is_empty(self, specs: list[str]) -> bool:
        """Detect a contradictory two-sided combined complement (an empty allow-set).

        Each spec here is a single-comparator complement the compiler produced from
        a deny (so the combined form is always bounds-only). When the specs reduce
        to a lower bound (>=A / >A) AND an upper bound (<B / <=B) with A and B
        comparable dotted-numeric versions and the bounds cross (lower > upper, or
        touch with an exclusive endpoint), the allow-set is empty and the package
        would be silently blocked whole. Conservative: only flags a provable
        contradiction.
        """
        lower: tuple[tuple[int, ...], bool] | None = None  # (version, inclusive)
        upper: tuple[tuple[int, ...], bool] | None = None
        for spec in specs:
            m = _PEP440_SINGLE_CMP_RE.match(spec)
            if not m:
                return False  # not a simple bound (e.g. != complement); cannot decide
            op, ver = m.group(1), m.group(2)
            if op == "==":
                return False  # an equality is not a one-sided bound
            rel = self._release_tuple(ver)
            if rel is None:
                return False
            if op in (">", ">="):
                cand = (rel, op == ">=")
                if lower is None or cand[0] > lower[0]:
                    lower = cand
            else:  # < or <=
                cand = (rel, op == "<=")
                if upper is None or cand[0] < upper[0]:
                    upper = cand
        if lower is None or upper is None:
            return False
        lo_v, lo_incl = lower
        up_v, up_incl = upper
        if lo_v > up_v:
            return True
        if lo_v == up_v and not (lo_incl and up_incl):
            return True
        return False


# The adapter concept is the ADR-0007 extension seam; only one instance of each
# is ever needed (they are stateless), so expose them as module constants rather
# than via a dispatch registry.
NPM = NpmAdapter()
PYPI = PypiAdapter()
