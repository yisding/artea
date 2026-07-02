"""Inline OSV.dev malicious-package decisions.

Artea does not mirror the OSV database. Enforcement points ask policy-sync about
the package versions they are already serving, and policy-sync translates that to
fast package-level OSV queries with querybatch fallback and small bounded verdict
caches.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .adapters import NPM, PYPI, NpmAdapter, PypiAdapter
from .config import DEFAULT_OSV_API_URL
from .policy_model import Action, Policy, PolicyError

log = logging.getLogger(__name__)

MAL_PREFIX = "MAL-"
# Accepted decide() ecosystem input -> adapter. Each adapter carries its internal
# id (adapter.ecosystem) and OSV.dev casing (adapter.osv_ecosystem), so this is
# the single mapping to update when adding/renaming an ecosystem. "PyPI" is a
# required alias: callers may pass the OSV casing as well as the internal "pypi".
ADAPTERS: dict[str, NpmAdapter | PypiAdapter] = {"npm": NPM, "pypi": PYPI, "PyPI": PYPI}
MAX_PAGES = 8
PACKAGE_BATCH_DELAY_SECONDS = 0.005
SLOW_OSV_REQUEST_SECONDS = 0.2
_SEMVER_RE = re.compile(
    r"^v?(0|[1-9]\d*)"
    r"(?:\.(0|[1-9]\d*))?"
    r"(?:\.(0|[1-9]\d*))?"
    r"(?:-([0-9A-Za-z.-]+))?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)
_SEMVER_NUMERIC_ID_RE = re.compile(r"^(0|[1-9]\d*)$")


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


@dataclass
class _PendingPackageQuery:
    osv_ecosystem: str
    name: str
    event: threading.Event
    result: dict | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class _CachedPackagePage:
    stored_at: float
    data: dict
    has_malicious: bool


class _VerdictCache:
    def __init__(
        self,
        positive_ttl: float,
        negative_ttl: float,
        now=time.time,
        max_entries: int = 131072,
        cache_file_path: str = "",
    ):
        self.positive_ttl = positive_ttl
        self.negative_ttl = negative_ttl
        self.now = now
        self.max_entries = max_entries
        self.cache_file_path = cache_file_path
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str, str], _CachedVerdict] = {}
        if cache_file_path:
            self._load()

    def get(self, key: tuple[str, str, str]) -> OsvVerdict | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if self._is_expired(entry, self.now()):
                return None
            return entry.verdict

    def set(self, key: tuple[str, str, str], verdict: OsvVerdict) -> None:
        with self._lock:
            now = self.now()
            self._data[key] = _CachedVerdict(stored_at=now, verdict=verdict)
            if len(self._data) <= self.max_entries:
                return
            for old_key, entry in list(self._data.items()):
                if self._is_expired(entry, now):
                    del self._data[old_key]
            overflow = len(self._data) - self.max_entries
            if overflow > 0:
                # Evict clean (negative) verdicts before malicious ones so a
                # still-valid MAL- verdict keeps blocking through an OSV outage,
                # as the fail-open contract documents. (blocked False < True.)
                for old_key, _entry in sorted(
                    self._data.items(), key=lambda kv: (kv[1].verdict.blocked, kv[1].stored_at)
                )[:overflow]:
                    del self._data[old_key]

    def persist(self) -> None:
        if not self.cache_file_path:
            return
        with self._lock:
            now = self.now()
            entries = []
            for key, entry in list(self._data.items()):
                if self._is_expired(entry, now):
                    del self._data[key]
                    continue
                ecosystem, name, version = key
                entries.append(
                    {
                        "key": [ecosystem, name, version],
                        "stored_at": entry.stored_at,
                        "blocked": entry.verdict.blocked,
                        "ids": list(entry.verdict.ids),
                    }
                )
            try:
                directory = os.path.dirname(self.cache_file_path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                tmp_path = f"{self.cache_file_path}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump({"entries": entries}, f, separators=(",", ":"))
                os.replace(tmp_path, self.cache_file_path)
            except OSError:
                log.exception("failed to persist OSV verdict cache to %s", self.cache_file_path)

    def _load(self) -> None:
        try:
            with open(self.cache_file_path, encoding="utf-8") as f:
                payload = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.exception("failed to load OSV verdict cache from %s", self.cache_file_path)
            return
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return
        now = self.now()
        loaded = 0
        with self._lock:
            for raw in entries:
                if not isinstance(raw, dict):
                    continue
                key = raw.get("key")
                if (
                    not isinstance(key, list)
                    or len(key) != 3
                    or not all(isinstance(part, str) and part for part in key)
                ):
                    continue
                stored_at = raw.get("stored_at")
                blocked = raw.get("blocked")
                ids = raw.get("ids")
                if not isinstance(stored_at, (int, float)) or not isinstance(blocked, bool) or not isinstance(ids, list):
                    continue
                verdict = OsvVerdict(version=key[2], blocked=blocked, ids=tuple(id_ for id_ in ids if isinstance(id_, str)))
                entry = _CachedVerdict(stored_at=float(stored_at), verdict=verdict)
                if self._is_expired(entry, now):
                    continue
                self._data[(key[0], key[1], key[2])] = entry
                loaded += 1
                if loaded >= self.max_entries:
                    break
        if loaded:
            log.info("loaded %s OSV verdict cache entries from %s", loaded, self.cache_file_path)

    def _is_expired(self, entry: _CachedVerdict, now: float) -> bool:
        ttl = self.positive_ttl if entry.verdict.blocked else self.negative_ttl
        return now - entry.stored_at >= ttl


class OsvClient:
    def __init__(
        self,
        api_url: str = DEFAULT_OSV_API_URL,
        timeout: float = 5.0,
        positive_ttl: float = 3600.0,
        negative_ttl: float = 900.0,
        batch_size: int = 100,
        max_concurrency: int = 8,
        cache_file_path: str = "",
        now=time.time,
    ):
        if batch_size <= 0:
            raise ValueError("OSV batch size must be positive")
        if max_concurrency <= 0:
            raise ValueError("OSV max concurrency must be positive")
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.batch_size = batch_size
        self.max_concurrency = max_concurrency
        self._request_gate = threading.BoundedSemaphore(max_concurrency)
        self._package_batch_lock = threading.Lock()
        self._pending_package_queries: dict[tuple[str, str], _PendingPackageQuery] = {}
        self._package_batch_active = False
        self._package_page_cache_lock = threading.Lock()
        self._package_page_cache: dict[tuple[str, str], _CachedPackagePage] = {}
        self.cache = _VerdictCache(
            positive_ttl=positive_ttl,
            negative_ttl=negative_ttl,
            now=now,
            cache_file_path=cache_file_path,
        )

    def decide(
        self,
        policy: Policy | None,
        ecosystem: str,
        name: str,
        versions: list[str],
    ) -> OsvDecisionResult:
        adapter = ADAPTERS.get(ecosystem)
        if adapter is None:
            raise PolicyError(f"unknown ecosystem {ecosystem!r}")
        internal = adapter.ecosystem
        osv_ecosystem = adapter.osv_ecosystem
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
                self.cache.persist()

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

    def summarize_package(
        self,
        policy: Policy | None,
        ecosystem: str,
        name: str,
    ) -> OsvDecisionResult:
        adapter = ADAPTERS.get(ecosystem)
        if adapter is None:
            raise PolicyError(f"unknown ecosystem {ecosystem!r}")
        internal = adapter.ecosystem
        osv_ecosystem = adapter.osv_ecosystem
        normalized_name = adapter.normalize_name(name)

        if policy is None:
            return OsvDecisionResult(
                status="policy_unavailable",
                verdicts=(),
                reason="no parsed policy has synced yet",
            )
        if not policy.osv_malicious_packages:
            return OsvDecisionResult(
                status="disabled",
                verdicts=(),
                reason="osv.malicious_packages is disabled",
            )

        ids_by_version: dict[str, set[str]] = {}
        page_token: str | None = None
        pages = 0
        while True:
            pages += 1
            if pages > MAX_PAGES:
                raise OsvError("OSV package query pagination exceeded safety limit")
            if page_token is None:
                data = self._query_package_first_page(osv_ecosystem, normalized_name)
            else:
                data = self._post_json(
                    "/v1/query",
                    {
                        "package": {"name": normalized_name, "ecosystem": osv_ecosystem},
                        "page_token": page_token,
                    },
                )
            vulns = data.get("vulns") or []
            if not isinstance(vulns, list):
                raise OsvError("OSV package query response shape was invalid")
            for vuln in vulns:
                if not isinstance(vuln, dict):
                    continue
                vuln_id = vuln.get("id")
                if not (isinstance(vuln_id, str) and vuln_id.startswith(MAL_PREFIX)):
                    continue
                exact_versions = _exact_affected_versions(vuln)
                if exact_versions is None:
                    return OsvDecisionResult(
                        status="needs_versions",
                        verdicts=(),
                        reason="MAL record lacks exact affected versions",
                    )
                for version in exact_versions:
                    if _allow_override_applies(policy, internal, adapter, normalized_name, version):
                        continue
                    ids_by_version.setdefault(version, set()).add(vuln_id)

            token = data.get("next_page_token")
            if not (isinstance(token, str) and token):
                break
            page_token = token

        return OsvDecisionResult(
            status="ok",
            verdicts=tuple(
                OsvVerdict(version=version, blocked=True, ids=tuple(sorted(ids)))
                for version, ids in sorted(ids_by_version.items())
            ),
        )

    def _query_osv(self, osv_ecosystem: str, name: str, versions: list[str]) -> dict[str, OsvVerdict]:
        package_result = self._query_package(osv_ecosystem, name, versions)
        if package_result is not None:
            return package_result
        return self._query_osv_versions(osv_ecosystem, name, versions)

    def _query_osv_versions(self, osv_ecosystem: str, name: str, versions: list[str]) -> dict[str, OsvVerdict]:
        chunks = [versions[start:start + self.batch_size] for start in range(0, len(versions), self.batch_size)]
        if len(chunks) <= 1:
            return self._query_chunk(osv_ecosystem, name, versions)

        out: dict[str, OsvVerdict] = {}
        with ThreadPoolExecutor(max_workers=min(self.max_concurrency, len(chunks))) as executor:
            futures = [
                executor.submit(self._query_chunk, osv_ecosystem, name, chunk)
                for chunk in chunks
            ]
            for future in as_completed(futures):
                out.update(future.result())
        return out

    def _query_package(self, osv_ecosystem: str, name: str, versions: list[str]) -> dict[str, OsvVerdict] | None:
        """Fast MAL-only package query.

        OSV package queries without a version return all records for the package.
        Artea only blocks `MAL-*` records, so packages with no malicious records
        can be decided with one package-level request instead of one query per
        candidate version. Concurrent package-level requests are briefly batched
        through `/v1/querybatch`. When a MAL record exposes exact affected
        versions, intersect it locally with the candidate versions. If a MAL
        record lacks an exact version list, return None so the existing
        per-version querybatch path makes the authoritative decision.
        """
        first_page = self._query_package_first_page(osv_ecosystem, name)
        if first_page.get("next_page_token"):
            return self._query_package_unbatched(osv_ecosystem, name, versions)
        return self._verdicts_from_package_page(osv_ecosystem, first_page, versions)

    def _query_package_unbatched(self, osv_ecosystem: str, name: str, versions: list[str]) -> dict[str, OsvVerdict] | None:
        wanted = set(versions)
        ids_by_version: dict[str, set[str]] = {version: set() for version in versions}
        page_token: str | None = None
        pages = 0
        while True:
            pages += 1
            if pages > MAX_PAGES:
                raise OsvError("OSV package query pagination exceeded safety limit")
            payload: dict = {"package": {"name": name, "ecosystem": osv_ecosystem}}
            if page_token:
                payload["page_token"] = page_token
            data = self._post_json("/v1/query", payload)
            vulns = data.get("vulns") or []
            if not isinstance(vulns, list):
                raise OsvError("OSV package query response shape was invalid")

            if not _apply_package_vulns(osv_ecosystem, vulns, wanted, ids_by_version):
                return None

            token = data.get("next_page_token")
            if not (isinstance(token, str) and token):
                break
            page_token = token

        return _package_verdicts(versions, ids_by_version)

    def _query_package_first_page(self, osv_ecosystem: str, name: str) -> dict:
        key = (osv_ecosystem, name)
        cached = self._get_cached_package_page(key)
        if cached is not None:
            return cached
        leader = False
        with self._package_batch_lock:
            pending = self._pending_package_queries.get(key)
            if pending is None:
                pending = _PendingPackageQuery(osv_ecosystem=osv_ecosystem, name=name, event=threading.Event())
                self._pending_package_queries[key] = pending
            if not self._package_batch_active:
                self._package_batch_active = True
                leader = True

        if leader:
            time.sleep(PACKAGE_BATCH_DELAY_SECONDS)
            with self._package_batch_lock:
                batch = list(self._pending_package_queries.values())
                self._pending_package_queries.clear()
                self._package_batch_active = False
            self._resolve_package_batch(batch)
        else:
            pending.event.wait()

        if pending.error is not None:
            raise pending.error
        if not isinstance(pending.result, dict):
            raise OsvError("OSV package query response shape was invalid")
        return pending.result

    def _get_cached_package_page(self, key: tuple[str, str]) -> dict | None:
        with self._package_page_cache_lock:
            entry = self._package_page_cache.get(key)
            if entry is None:
                return None
            ttl = self.cache.positive_ttl if entry.has_malicious else self.cache.negative_ttl
            if self.cache.now() - entry.stored_at >= ttl:
                del self._package_page_cache[key]
                return None
            return entry.data

    def _store_package_page(self, key: tuple[str, str], data: dict) -> None:
        with self._package_page_cache_lock:
            self._package_page_cache[key] = _CachedPackagePage(
                stored_at=self.cache.now(),
                data=data,
                has_malicious=_page_has_malicious_record(data),
            )
            if len(self._package_page_cache) <= self.cache.max_entries:
                return
            overflow = len(self._package_page_cache) - self.cache.max_entries
            for old_key, _entry in sorted(self._package_page_cache.items(), key=lambda kv: kv[1].stored_at)[:overflow]:
                del self._package_page_cache[old_key]

    def _resolve_package_batch(self, batch: list[_PendingPackageQuery]) -> None:
        try:
            for start in range(0, len(batch), self.batch_size):
                chunk = batch[start:start + self.batch_size]
                data = self._post_json(
                    "/v1/querybatch",
                    {
                        "queries": [
                            {"package": {"name": pending.name, "ecosystem": pending.osv_ecosystem}}
                            for pending in chunk
                        ]
                    },
                )
                results = data.get("results")
                if not isinstance(results, list) or len(results) != len(chunk):
                    raise OsvError("OSV package querybatch response shape was invalid")
                for pending, result in zip(chunk, results, strict=True):
                    if not isinstance(result, dict):
                        raise OsvError("OSV package querybatch result shape was invalid")
                    pending.result = result
                    self._store_package_page((pending.osv_ecosystem, pending.name), result)
        except BaseException as e:
            for pending in batch:
                pending.error = e
        finally:
            for pending in batch:
                pending.event.set()

    def _verdicts_from_package_page(self, osv_ecosystem: str, data: dict, versions: list[str]) -> dict[str, OsvVerdict] | None:
        vulns = data.get("vulns") or []
        if not isinstance(vulns, list):
            raise OsvError("OSV package query response shape was invalid")
        ids_by_version: dict[str, set[str]] = {version: set() for version in versions}
        if not _apply_package_vulns(osv_ecosystem, vulns, set(versions), ids_by_version):
            return None
        return _package_verdicts(versions, ids_by_version)

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
        started = time.perf_counter()
        with self._request_gate:
            data = self._post_json_unbounded(path, payload)
        elapsed = time.perf_counter() - started
        if elapsed >= SLOW_OSV_REQUEST_SECONDS:
            query_count = len(payload.get("queries", [])) if isinstance(payload.get("queries"), list) else 1
            log.info(
                "OSV upstream %s queries=%s elapsed_ms=%.1f",
                path,
                query_count,
                elapsed * 1000,
            )
        return data

    def _post_json_unbounded(self, path: str, payload: dict) -> dict:
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


def _exact_affected_versions(vuln: dict) -> set[str] | None:
    """Return exact affected versions from an OSV record, or None if unavailable.

    A None result means "cannot decide locally"; callers must fall back to
    per-version OSV queries. An empty set is a valid exact result: a malformed or
    unrelated MAL record with no string versions blocks nothing locally.
    """
    affected = vuln.get("affected") or []
    if not isinstance(affected, list):
        return None
    out: set[str] = set()
    saw_exact_list = False
    for item in affected:
        if not isinstance(item, dict):
            continue
        raw_versions = item.get("versions")
        if isinstance(raw_versions, list):
            saw_exact_list = True
            out.update(version for version in raw_versions if isinstance(version, str) and version)
            continue
        # A MAL record with ranges but no exact versions needs the authoritative
        # per-version querybatch path so we do not under-block.
        if item.get("ranges"):
            return None
    return out if saw_exact_list else None


def _page_has_malicious_record(data: dict) -> bool:
    vulns = data.get("vulns") or []
    if not isinstance(vulns, list):
        return False
    return any(
        isinstance(vuln, dict)
        and isinstance(vuln.get("id"), str)
        and vuln["id"].startswith(MAL_PREFIX)
        for vuln in vulns
    )


def _apply_package_vulns(osv_ecosystem: str, vulns: list, wanted: set[str], ids_by_version: dict[str, set[str]]) -> bool:
    for vuln in vulns:
        if not isinstance(vuln, dict):
            continue
        vuln_id = vuln.get("id")
        if not (isinstance(vuln_id, str) and vuln_id.startswith(MAL_PREFIX)):
            continue
        affected_versions = _locally_affected_versions(osv_ecosystem, vuln, wanted)
        if affected_versions is None:
            return False
        for version in wanted.intersection(affected_versions):
            ids_by_version[version].add(vuln_id)
    return True


def _locally_affected_versions(osv_ecosystem: str, vuln: dict, wanted: set[str]) -> set[str] | None:
    affected = vuln.get("affected") or []
    if not isinstance(affected, list):
        return None
    out: set[str] = set()
    saw_decidable_affected = False
    for item in affected:
        if not isinstance(item, dict):
            continue
        raw_versions = item.get("versions")
        if isinstance(raw_versions, list):
            saw_decidable_affected = True
            out.update(version for version in raw_versions if isinstance(version, str) and version)
        raw_ranges = item.get("ranges")
        if raw_ranges:
            if osv_ecosystem != "npm":
                return None
            if not isinstance(raw_ranges, list):
                return None
            saw_decidable_affected = True
            for raw_range in raw_ranges:
                if not isinstance(raw_range, dict):
                    return None
                affected_by_range = _semver_range_affected_versions(raw_range, wanted)
                if affected_by_range is None:
                    return None
                out.update(affected_by_range)
    return out if saw_decidable_affected else None


def _semver_range_affected_versions(raw_range: dict, wanted: set[str]) -> set[str] | None:
    if raw_range.get("type") != "SEMVER":
        return None
    events = raw_range.get("events")
    if not isinstance(events, list):
        return None
    candidate_versions: dict[str, tuple[int, int, int, tuple[int | str, ...] | None]] = {}
    for version in wanted:
        parsed = _parse_semver(version)
        if parsed is None:
            return None
        candidate_versions[version] = parsed

    affected: set[str] = set()
    for version, parsed_version in candidate_versions.items():
        is_affected = False
        for event in events:
            if not isinstance(event, dict) or len(event) != 1:
                return None
            kind, raw_event_version = next(iter(event.items()))
            if not isinstance(raw_event_version, str):
                return None
            parsed_event_version = _parse_semver(raw_event_version)
            if parsed_event_version is None:
                return None
            comparison = _compare_semver(parsed_version, parsed_event_version)
            if kind == "introduced":
                if comparison >= 0:
                    is_affected = True
            elif kind in {"fixed", "limit"}:
                if comparison >= 0:
                    is_affected = False
            elif kind == "last_affected":
                if comparison > 0:
                    is_affected = False
            else:
                return None
        if is_affected:
            affected.add(version)
    return affected


def _parse_semver(version: str) -> tuple[int, int, int, tuple[int | str, ...] | None] | None:
    match = _SEMVER_RE.match(version)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2) or "0")
    patch = int(match.group(3) or "0")
    prerelease_raw = match.group(4)
    prerelease: tuple[int | str, ...] | None = None
    if prerelease_raw:
        parts: list[int | str] = []
        for part in prerelease_raw.split("."):
            if not part:
                return None
            parts.append(int(part) if _SEMVER_NUMERIC_ID_RE.match(part) else part)
        prerelease = tuple(parts)
    return major, minor, patch, prerelease


def _compare_semver(
    left: tuple[int, int, int, tuple[int | str, ...] | None],
    right: tuple[int, int, int, tuple[int | str, ...] | None],
) -> int:
    left_core = left[:3]
    right_core = right[:3]
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    return _compare_prerelease(left[3], right[3])


def _compare_prerelease(left: tuple[int | str, ...] | None, right: tuple[int | str, ...] | None) -> int:
    if left is None and right is None:
        return 0
    if left is None:
        return 1
    if right is None:
        return -1
    for left_part, right_part in zip(left, right, strict=False):
        if left_part == right_part:
            continue
        left_is_num = isinstance(left_part, int)
        right_is_num = isinstance(right_part, int)
        if left_is_num and right_is_num:
            return -1 if left_part < right_part else 1
        if left_is_num:
            return -1
        if right_is_num:
            return 1
        return -1 if left_part < right_part else 1
    if len(left) == len(right):
        return 0
    return -1 if len(left) < len(right) else 1


def _package_verdicts(versions: list[str], ids_by_version: dict[str, set[str]]) -> dict[str, OsvVerdict]:
    return {
        version: OsvVerdict(
            version=version,
            blocked=bool(ids_by_version[version]),
            ids=tuple(sorted(ids_by_version[version])),
        )
        for version in versions
    }


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
        if adapter.is_exact(rule.versions) and adapter.exact_allows(rule.versions, version):
            return True
    return False


def response_payload(result: OsvDecisionResult, blocked_only: bool = False) -> dict:
    verdicts = [verdict for verdict in result.verdicts if verdict.blocked] if blocked_only else result.verdicts
    payload: dict = {
        "status": result.status,
        "results": [
            {"version": verdict.version, "blocked": verdict.blocked, "ids": list(verdict.ids)}
            for verdict in verdicts
        ],
    }
    if result.reason:
        payload["reason"] = result.reason
    return payload
