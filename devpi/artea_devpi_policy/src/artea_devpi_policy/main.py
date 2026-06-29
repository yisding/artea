"""Constrained devpi index with Artea upstream age policy.

This is derived from devpi-constrained's compact stage customizer and keeps the
same `type=constrained` index contract. Artea adds `min_upstream_age`, an ISO
8601 duration used to hide public PyPI versions/files until PyPI upload
metadata proves they are old enough.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from devpi_common.metadata import parse_requirement, splitext_archive
from devpi_common.types import cached_property
from devpi_common.validation import normalize_name
from packaging_legacy.version import LegacyVersion
from packaging_legacy.version import parse as parse_version
from pluggy import HookimplMarker
from pyramid.httpexceptions import HTTPBadRequest, HTTPForbidden, HTTPNoContent
import pkg_resources

server_hookimpl = HookimplMarker("devpiserver")
log = logging.getLogger(__name__)

DEFAULT_PYPI_JSON_URL = "https://pypi.org/pypi"
DEFAULT_METADATA_CACHE_SECONDS = 300.0
CONSTRAINED_INDEX = "root/constrained"
PYPI_MIRROR_INDEX = "root/pypi"
PYPI_FILE_PREFIXES = ("root/pypi/+f/", "root/pypi/+e/")
# PEP 658/714: devpi serves a distribution's Core Metadata at the file URL with
# `.metadata` appended (e.g. `<wheel>.whl.metadata`). devpi has no separate link
# entry for it, so policy decisions resolve the underlying wheel link by stripping
# this suffix — a metadata file is allowed iff its distribution is.
METADATA_SUFFIX = ".metadata"
OSV_TIMEOUT_SECONDS = 5

ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<weeks>\d+(?:\.\d+)?)W)?(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)
ISO_FACTORS = {
    "weeks": 7 * 24 * 60 * 60,
    "days": 24 * 60 * 60,
    "hours": 60 * 60,
    "minutes": 60,
    "seconds": 1,
}


class ConstraintsDict(dict):
    constrain_all = False


class MetadataUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ProjectMetadata:
    fetched_at: float
    files: dict[str, float]
    versions: dict[str, list[float]]


metadata_cache: dict[tuple[str, str], ProjectMetadata] = {}


def default_osv_url() -> str:
    return os.environ.get("ARTEA_OSV_URL", "").strip()


def parse_iso_duration_seconds(raw: Any) -> float:
    if raw in (None, "", 0):
        return 0.0
    if not isinstance(raw, str):
        raise ValueError("min_upstream_age must be an ISO 8601 duration string")
    match = ISO_DURATION_RE.match(raw.strip())
    if not match or not any(match.groupdict().values()):
        raise ValueError("min_upstream_age must use ISO 8601 duration syntax such as P3D or PT72H")
    seconds = 0.0
    for key, factor in ISO_FACTORS.items():
        value = match.group(key)
        if value is not None:
            seconds += float(value) * factor
    if seconds < 0:
        raise ValueError("min_upstream_age must be non-negative")
    return seconds


def constraint_lines(text: str) -> list[str]:
    """Reduce raw constraint text to its effective lines: stripped, minus blanks
    and #-comment lines. One definition of an effective constraint line shared by
    parse_constraints and normalize_indexconfig_value."""
    result = []
    for item in text.splitlines():
        item = item.strip()
        if not item or item.startswith("#"):
            continue
        result.append(item)
    return result


def parse_constraints(constraints):
    result = ConstraintsDict()
    if isinstance(constraints, str):
        constraints = constraint_lines(constraints)
    for constraint in constraints:
        if constraint == "*":
            result.constrain_all = True
            continue
        try:
            constraint = parse_requirement(constraint)
        except pkg_resources.RequirementParseError as e:
            raise pkg_resources.RequirementParseError("%s for %r" % (e, constraint))
        if constraint.project_name in result:
            raise ValueError("Constraint for '%s' already exists." % constraint.project_name)
        result[constraint.project_name] = constraint.specifier
    return result


def iso_to_epoch(raw: str) -> float | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def filename_from_path(path: str) -> str:
    return urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1].split("#", 1)[0])


def link_entrypath(path: str) -> str:
    """Resolve a mirror-file request path to the entrypath of its distribution.

    For a PEP 658 Core Metadata request (`<file>.metadata`) this strips the
    suffix so the underlying wheel's mirror link is found and the SAME policy
    (constraints, upstream age, OSV) decides the metadata file too. Plain file
    paths are returned unchanged."""
    if path.endswith(METADATA_SUFFIX):
        return path[: -len(METADATA_SUFFIX)]
    return path


def fetch_project_metadata(project: str, pypi_json_url: str, now=time.time) -> ProjectMetadata:
    cache_key = (pypi_json_url, project)
    cached = metadata_cache.get(cache_key)
    if cached is not None and now() - cached.fetched_at < DEFAULT_METADATA_CACHE_SECONDS:
        return cached
    url = f"{pypi_json_url.rstrip('/')}/{urllib.parse.quote(project)}/json"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise MetadataUnavailable(f"{url}: {e}") from e

    files: dict[str, float] = {}
    versions: dict[str, list[float]] = {}
    releases = data.get("releases")
    if isinstance(releases, dict):
        for version, release_files in releases.items():
            if not isinstance(release_files, list):
                continue
            for item in release_files:
                if not isinstance(item, dict):
                    continue
                filename = item.get("filename")
                uploaded_raw = item.get("upload_time_iso_8601") or item.get("upload_time")
                if not isinstance(filename, str) or not isinstance(uploaded_raw, str):
                    continue
                uploaded = iso_to_epoch(uploaded_raw)
                if uploaded is None:
                    continue
                files[filename] = uploaded
                versions.setdefault(str(version), []).append(uploaded)
    metadata = ProjectMetadata(fetched_at=now(), files=files, versions=versions)
    metadata_cache[cache_key] = metadata
    return metadata


def query_osv_blocked_versions(osv_url: str, project: str, versions: list[str]) -> set[str]:
    unique = sorted({s for s in (str(version) for version in versions) if s})
    if not osv_url or not unique:
        return set()
    payload = json.dumps({"ecosystem": "pypi", "name": project, "versions": unique}).encode()
    req = urllib.request.Request(
        osv_url,
        data=payload,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OSV_TIMEOUT_SECONDS) as resp:
            data = json.load(resp)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.warning("OSV lookup unavailable for %s; failing open: %s", project, e)
        return set()
    blocked: set[str] = set()
    results = data.get("results")
    if not isinstance(results, list):
        log.warning("OSV lookup for %s returned invalid response; failing open", project)
        return set()
    for item in results:
        if not isinstance(item, dict):
            continue
        version = item.get("version")
        if item.get("blocked") is True and isinstance(version, str):
            blocked.add(version)
    return blocked


class ConstrainedStage:
    readonly = True

    def get_possible_indexconfig_keys(self):
        return ("constraints", "min_upstream_age", "pypi_json_url", "osv_url")

    def get_default_config_items(self):
        return [
            ("constraints", []),
            ("min_upstream_age", "P0D"),
            ("pypi_json_url", DEFAULT_PYPI_JSON_URL),
            ("osv_url", ""),
        ]

    def normalize_indexconfig_value(self, key, value):
        if key == "constraints":
            if not isinstance(value, list):
                return constraint_lines(value)
            return value
        if key == "min_upstream_age":
            parse_iso_duration_seconds(value)
            return value
        if key == "pypi_json_url":
            if not isinstance(value, str) or not value.strip():
                raise self.InvalidIndexconfig(["pypi_json_url must be a non-empty URL"])
            return value.rstrip("/")
        if key == "osv_url":
            if value in (None, ""):
                return ""
            if not isinstance(value, str):
                raise self.InvalidIndexconfig(["osv_url must be a URL string"])
            return value.rstrip("/")
        return value

    def validate_config(self, oldconfig, newconfig):
        errors = []
        try:
            parse_constraints(newconfig["constraints"])
        except Exception as e:
            errors.append("Error while parsing constraints: %s" % e)
        try:
            parse_iso_duration_seconds(newconfig.get("min_upstream_age", "P0D"))
        except Exception as e:
            errors.append("Error while parsing min_upstream_age: %s" % e)
        if errors:
            raise self.InvalidIndexconfig(errors)

    @cached_property
    def constraints(self):
        return parse_constraints(self.stage.ixconfig.get("constraints", ""))

    @cached_property
    def min_upstream_age_seconds(self):
        return parse_iso_duration_seconds(self.stage.ixconfig.get("min_upstream_age", "P0D"))

    @property
    def pypi_json_url(self):
        return self.stage.ixconfig.get("pypi_json_url") or DEFAULT_PYPI_JSON_URL

    @property
    def osv_url(self):
        return (self.stage.ixconfig.get("osv_url") or default_osv_url()).rstrip("/")

    def has_file_policy(self) -> bool:
        """True when any per-file gate (upstream age or OSV) is active, so the
        per-item filters and the file-age tween must inspect items individually
        rather than short-circuit."""
        return self.min_upstream_age_seconds > 0 or bool(self.osv_url)

    def get_projects_filter_iter(self, projects):
        constraints = self.constraints
        if not constraints.constrain_all:
            return
        for project in projects:
            yield project in constraints

    def _constraint_decision(self, project):
        """Resolve the shared opening decision for the per-project filters.

        Returns ``(version_filter, include_legacy, needs_age, needs_osv)`` when
        the caller must inspect items individually, or ``None`` as a sentinel
        meaning "this project is unconstrained and not age-gated, so express no
        opinion" (iters yield nothing, ``link_allowed`` returns ``True``). When
        the whole index is constrained but the project is not listed, this raises
        the per-item decision by returning a tuple whose ``version_filter`` is
        None with ``constrain_all`` active, which the per-item filters resolve to
        an immediate deny before fetching metadata or querying OSV.
        """
        constraints = self.constraints
        version_filter = constraints.get(project)
        if version_filter is None:
            if not constraints.constrain_all and not self.has_file_policy():
                return None
        include_legacy = version_filter is None or not len(version_filter)
        needs_age = self.min_upstream_age_seconds > 0
        needs_osv = bool(self.osv_url)
        return version_filter, include_legacy, needs_age, needs_osv

    def _filter_iter(self, project, items, version_of, age_ok_of):
        """Generic per-item filter shared by versions and simple-links iters.

        ``version_of(item)`` extracts the version (or ``None`` to deny the item)
        and ``age_ok_of(metadata, item)`` is the age predicate to apply. The
        fail-closed contract is preserved: an unknown timestamp makes the age
        predicate return ``False`` and the item is blocked.
        """
        constraints = self.constraints
        decision = self._constraint_decision(project)
        if decision is None:
            return
        version_filter, include_legacy, needs_age, needs_osv = decision
        if version_filter is None and constraints.constrain_all:
            for _item in items:
                yield False
            return
        metadata = self._project_metadata(project) if needs_age else None
        pending: list[tuple[Any, str | None, bool]] = []
        for item in items:
            version = version_of(item)
            if version is None:
                pending.append((item, None, False))
                continue
            if not self._version_matches_filter(version, version_filter, constraints, include_legacy):
                pending.append((item, str(version), False))
                continue
            if metadata is not None and not age_ok_of(metadata, item):
                pending.append((item, str(version), False))
                continue
            pending.append((item, str(version), True))
        blocked = (
            query_osv_blocked_versions(
                self.osv_url,
                project,
                [
                    version
                    for _item, version, allowed in pending
                    if allowed and version is not None
                ],
            )
            if needs_osv
            else set()
        )
        for _item, version, allowed in pending:
            yield allowed and version not in blocked

    def get_versions_filter_iter(self, project, versions):
        return self._filter_iter(
            project,
            versions,
            version_of=lambda version: version,
            age_ok_of=lambda metadata, version: self._version_old_enough(metadata, str(version)),
        )

    def get_simple_links_filter_iter(self, project, links):
        return self._filter_iter(
            project,
            links,
            version_of=lambda link_info: self._link_version(project, link_info),
            age_ok_of=lambda metadata, link_info: self._file_old_enough(metadata, filename_from_link(link_info)),
        )

    def link_allowed(self, project: str, link_info) -> bool:
        constraints = self.constraints
        decision = self._constraint_decision(project)
        if decision is None:
            return True
        version_filter, include_legacy, needs_age, needs_osv = decision
        version = self._link_version(project, link_info)
        if version is None:
            return False
        if not self._version_matches_filter(version, version_filter, constraints, include_legacy):
            return False
        if needs_age:
            metadata = self._project_metadata(project)
            if not self._file_old_enough(metadata, filename_from_link(link_info)):
                return False
        if needs_osv and str(version) in query_osv_blocked_versions(self.osv_url, project, [str(version)]):
            return False
        return True

    def _project_metadata(self, project: str) -> ProjectMetadata:
        try:
            return fetch_project_metadata(project, self.pypi_json_url)
        except MetadataUnavailable as e:
            log.warning("pypi metadata unavailable for %s: %s", project, e)
            return ProjectMetadata(fetched_at=time.time(), files={}, versions={})

    def _version_old_enough(self, metadata: ProjectMetadata, version: str) -> bool:
        uploads = metadata.versions.get(version) or []
        return any(time.time() - uploaded >= self.min_upstream_age_seconds for uploaded in uploads)

    def _file_old_enough(self, metadata: ProjectMetadata, filename: str) -> bool:
        uploaded = metadata.files.get(filename)
        return uploaded is not None and time.time() - uploaded >= self.min_upstream_age_seconds

    def _version_matches_filter(self, version, version_filter, constraints, include_legacy):
        if version_filter is None:
            return not constraints.constrain_all
        parsed_version = parse_version(version) if isinstance(version, str) else version
        if isinstance(parsed_version, LegacyVersion):
            return include_legacy
        return parsed_version in version_filter

    def _link_version(self, project, link_info):
        if isinstance(link_info, tuple):
            key = link_info[0]
            parts = splitext_archive(key)[0].split("-")
            for index in range(1, len(parts)):
                name = normalize_name("-".join(parts[:index]))
                if name != project:
                    continue
                return "-".join(parts[index:])
            return None
        link_project = getattr(link_info, "project", None) or getattr(link_info, "name", None)
        if link_project is None or normalize_name(link_project) != project:
            return None
        return link_info.version


def filename_from_link(link_info) -> str:
    if isinstance(link_info, tuple):
        return filename_from_path(link_info[1] if len(link_info) > 1 else link_info[0])
    return link_info.basename


def constrained_customizer(registry):
    stage = registry["xom"].model.getstage(CONSTRAINED_INDEX)
    if stage is None or getattr(stage, "customizer", None) is None:
        return None
    customizer = stage.customizer
    if not hasattr(customizer, "link_allowed"):
        return None
    return customizer


def file_age_tween_factory(handler, registry):
    def tween(request):
        path = request.path_info.lstrip("/")
        if not path.startswith(PYPI_FILE_PREFIXES):
            return handler(request)
        customizer = constrained_customizer(registry)
        if customizer is None or not customizer.has_file_policy():
            return handler(request)
        mirror = registry["xom"].model.getstage(PYPI_MIRROR_INDEX)
        link = mirror.get_link_from_entrypath(link_entrypath(path)) if mirror is not None else None
        project = getattr(link, "project", None)
        if link is None or not project:
            raise HTTPForbidden("public PyPI file requires age-verifiable mirror metadata")
        filename = getattr(link, "basename", filename_from_path(path))
        if not customizer.link_allowed(project, link):
            raise HTTPForbidden("%s is blocked by current constraints, upstream age policy, or OSV malicious-package policy" % filename)
        return handler(request)

    return tween


def pypi_file_allowed_view(request):
    raw_path = request.params.get("path", "")
    path = raw_path.lstrip("/")
    if not path.startswith(PYPI_FILE_PREFIXES):
        raise HTTPBadRequest("path must point at a public PyPI mirror file")

    customizer = constrained_customizer(request.registry)
    mirror = request.registry["xom"].model.getstage(PYPI_MIRROR_INDEX)
    link = mirror.get_link_from_entrypath(link_entrypath(path)) if mirror is not None else None
    if customizer is None or link is None:
        raise HTTPForbidden("public PyPI file is not in the mirror index")

    # The project is derived from devpi's own mirror metadata, never from the
    # request: the gateway forwards only the file path, so the policy decision is
    # made against the file's actual project (no fragile filename parsing in njs).
    project = normalize_name(getattr(link, "project", "") or getattr(link, "name", ""))
    if not project:
        raise HTTPForbidden("public PyPI file requires age-verifiable mirror metadata")

    if not customizer.link_allowed(project, link):
        raise HTTPForbidden("public PyPI file is blocked by current constraints or upstream age policy")

    return HTTPNoContent()


@server_hookimpl
def devpiserver_pyramid_configure(config, pyramid_config):
    pyramid_config.add_tween("artea_devpi_policy.main.file_age_tween_factory")
    pyramid_config.add_route("artea_pypi_file_allowed", "/+artea/file-allowed")
    pyramid_config.add_view(pypi_file_allowed_view, route_name="artea_pypi_file_allowed", request_method="GET")


@server_hookimpl
def devpiserver_get_stage_customizer_classes():
    return [("constrained", ConstrainedStage)]
