"""Inline OSV.dev malicious-package decisions.

Artea does not mirror the OSV database. Enforcement points ask policy-sync about
the package versions they are already serving, and policy-sync translates that to
OSV querybatch calls with small bounded verdict caches.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .adapters import NPM, PYPI, NpmAdapter, PypiAdapter
from .policy_model import Action, Policy, PolicyError

log = logging.getLogger(__name__)

MAL_PREFIX = "MAL-"
OSV_ECOSYSTEMS = {"npm": "npm", "pypi": "PyPI", "PyPI": "PyPI"}
INTERNAL_ECOSYSTEMS = {"npm": "npm", "pypi": "pypi", "PyPI": "pypi"}
ADAPTERS: dict[str, NpmAdapter | PypiAdapter] = {"npm": NPM, "pypi": PYPI}
MAX_PAGES = 8


class OsvError(Exception):
    pass


@dataclass(frozen=True)
class OsvVerdict:
    version: str
    blocked: bool
    ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class OsvDecisionResult:
    status: str
    verdicts: tuple[OsvVerdict, ...]
    reason: str | None = None


@dataclass(frozen=True)
class _CachedVerdict:
    stored_at: float
    verdict: OsvVerdict


class _VerdictCache:
    def __init__(
        self,
        positive_ttl: float,
        negative_ttl: float,
        now=time.time,
        max_entries: int = 16384,
    ):
        self.positive_ttl = positive_ttl
        self.negative_ttl = negative_ttl
        self.now = now
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str, str], _CachedVerdict] = {}

    def get(self, key: tuple[str, str, str]) -> OsvVerdict | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ttl = self.positive_ttl if entry.verdict.blocked else self.negative_ttl
            if self.now() - entry.stored_at >= ttl:
                return None
            return entry.verdict

    def set(self, key: tuple[str, str, str], verdict: OsvVerdict) -> None:
        with self._lock:
            now = self.now()
            self._data[key] = _CachedVerdict(stored_at=now, verdict=verdict)
            if len(self._data) <= self.max_entries:
                return
            for old_key, entry in list(self._data.items()):
                ttl = self.positive_ttl if entry.verdict.blocked else self.negative_ttl
                if now - entry.stored_at >= ttl:
                    del self._data[old_key]
            overflow = len(self._data) - self.max_entries
            if overflow > 0:
                for old_key, _entry in sorted(self._data.items(), key=lambda kv: kv[1].stored_at)[
                    :overflow
                ]:
                    del self._data[old_key]


class OsvClient:
    def __init__(
        self,
        api_url: str = "https://api.osv.dev",
        timeout: float = 5.0,
        positive_ttl: float = 3600.0,
        negative_ttl: float = 900.0,
        batch_size: int = 100,
        now=time.time,
    ):
        if batch_size <= 0:
            raise ValueError("OSV batch size must be positive")
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.batch_size = batch_size
        self.cache = _VerdictCache(positive_ttl=positive_ttl, negative_ttl=negative_ttl, now=now)

    def decide(
        self,
        policy: Policy | None,
        ecosystem: str,
        name: str,
        versions: list[str],
    ) -> OsvDecisionResult:
        internal = INTERNAL_ECOSYSTEMS.get(ecosystem)
        osv_ecosystem = OSV_ECOSYSTEMS.get(ecosystem)
        if internal is None or osv_ecosystem is None:
            raise PolicyError(f"unknown ecosystem {ecosystem!r}")
        adapter = ADAPTERS[internal]
        normalized_name = adapter.normalize_name(name)

        ordered_versions = _unique_versions(versions)
        if policy is None:
            return _all_allowed("policy_unavailable", ordered_versions, "no parsed policy has synced yet")
        if not policy.osv_malicious_packages:
            return _all_allowed("disabled", ordered_versions, "osv.malicious_packages is disabled")

        allowed = {
            version
            for version in ordered_versions
            if _allow_override_applies(policy, internal, adapter, normalized_name, version)
        }
        query_versions = [version for version in ordered_versions if version not in allowed]
        cached: dict[str, OsvVerdict] = {}
        misses: list[str] = []
        for version in query_versions:
            key = (internal, normalized_name, version)
            cached_verdict = self.cache.get(key)
            if cached_verdict is None:
                misses.append(version)
            else:
                cached[version] = cached_verdict

        status = "ok"
        reason = None
        fetched: dict[str, OsvVerdict] = {}
        if misses:
            try:
                fetched = self._query_osv(osv_ecosystem, normalized_name, misses)
            except OsvError as e:
                status = "degraded"
                reason = str(e)
                log.warning("OSV lookup failed for %s/%s: %s", internal, normalized_name, e)
            else:
                for version, verdict in fetched.items():
                    self.cache.set((internal, normalized_name, version), verdict)

        verdicts: list[OsvVerdict] = []
        for version in ordered_versions:
            if version in allowed:
                verdicts.append(OsvVerdict(version=version, blocked=False))
            elif version in cached:
                verdicts.append(cached[version])
            elif version in fetched:
                verdicts.append(fetched[version])
            else:
                # OSV is degraded and there is no positive cached verdict: fail open.
                verdicts.append(OsvVerdict(version=version, blocked=False))
        return OsvDecisionResult(status=status, verdicts=tuple(verdicts), reason=reason)

    def _query_osv(self, osv_ecosystem: str, name: str, versions: list[str]) -> dict[str, OsvVerdict]:
        out: dict[str, OsvVerdict] = {}
        for start in range(0, len(versions), self.batch_size):
            chunk = versions[start:start + self.batch_size]
            out.update(self._query_chunk(osv_ecosystem, name, chunk))
        return out

    def _query_chunk(self, osv_ecosystem: str, name: str, versions: list[str]) -> dict[str, OsvVerdict]:
        ids_by_version: dict[str, set[str]] = {version: set() for version in versions}
        pending = list(versions)
        page_tokens: dict[str, str] = {}
        pages = 0
        while pending:
            pages += 1
            if pages > MAX_PAGES:
                raise OsvError("OSV querybatch pagination exceeded safety limit")
            queries = []
            for version in pending:
                query: dict = {
                    "package": {"name": name, "ecosystem": osv_ecosystem},
                    "version": version,
                }
                token = page_tokens.get(version)
                if token:
                    query["page_token"] = token
                queries.append(query)

            data = self._post_json("/v1/querybatch", {"queries": queries})
            results = data.get("results")
            if not isinstance(results, list) or len(results) != len(pending):
                raise OsvError("OSV querybatch response shape was invalid")

            next_pending: list[str] = []
            for version, result in zip(pending, results, strict=True):
                if not isinstance(result, dict):
                    raise OsvError("OSV querybatch result shape was invalid")
                vulns = result.get("vulns") or []
                if not isinstance(vulns, list):
                    raise OsvError("OSV querybatch vulns shape was invalid")
                for vuln in vulns:
                    if not isinstance(vuln, dict):
                        continue
                    vuln_id = vuln.get("id")
                    if isinstance(vuln_id, str) and vuln_id.startswith(MAL_PREFIX):
                        ids_by_version[version].add(vuln_id)
                token = result.get("next_page_token")
                if isinstance(token, str) and token:
                    page_tokens[version] = token
                    next_pending.append(version)
            pending = next_pending

        return {
            version: OsvVerdict(
                version=version,
                blocked=bool(ids_by_version[version]),
                ids=tuple(sorted(ids_by_version[version])),
            )
            for version in versions
        }

    def _post_json(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.api_url}{path}",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "artea-policy-sync/0.1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            raise OsvError(f"OSV {path} returned HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise OsvError(f"OSV {path} failed: {e}") from e


def _unique_versions(versions: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for version in versions:
        if not isinstance(version, str) or not version:
            raise PolicyError("'versions' entries must be non-empty strings")
        if version not in seen:
            seen.add(version)
            out.append(version)
    return out


def _all_allowed(status: str, versions: list[str], reason: str) -> OsvDecisionResult:
    return OsvDecisionResult(
        status=status,
        reason=reason,
        verdicts=tuple(OsvVerdict(version=version, blocked=False) for version in versions),
    )


def _allow_override_applies(
    policy: Policy,
    ecosystem: str,
    adapter: NpmAdapter | PypiAdapter,
    normalized_name: str,
    version: str,
) -> bool:
    for rule in policy.rules:
        if rule.ecosystem != ecosystem or rule.action is not Action.ALLOW or rule.name is None:
            continue
        if adapter.normalize_name(rule.name) != normalized_name:
            continue
        if rule.versions is None:
            return True
        if adapter.is_exact(rule.versions) and adapter.exact_value(rule.versions) == version:
            return True
    return False


def response_payload(result: OsvDecisionResult) -> dict:
    payload: dict = {
        "status": result.status,
        "results": [
            {"version": verdict.version, "blocked": verdict.blocked, "ids": list(verdict.ids)}
            for verdict in result.verdicts
        ],
    }
    if result.reason:
        payload["reason"] = result.reason
    return payload
