"""TOOL-021 immutable, checksum-pinned OSDU schema catalogs."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from uuid import UUID, uuid4, uuid5

from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

from agentic_osdu.domain.models import SchemaCatalogRef
from agentic_osdu.policy import (
    NetworkAccessPolicy,
    NetworkApproval,
    NetworkPolicy,
    NetworkPurpose,
    NetworkRequest,
    PolicyViolation,
)
from agentic_osdu.tools.contracts import (
    ApprovedRemoteSchemaCatalogRefresh,
    LocalSchemaCatalogImport,
    SchemaChecksum,
)

_KIND_PATTERN = re.compile(
    r"^(?P<authority>[\w.-]+):(?P<source>[\w.-]+):"
    r"(?P<entity>[\w.-]+):(?P<version>\d+\.\d+\.\d+)$"
)
_REVISION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_CATALOG_NAMESPACE = UUID("5071ad4a-9de4-4ba9-ae36-4f2b4e17d67b")
_CATALOG_MANIFEST = "catalog.json"
_ACTIVE_MANIFEST = "active.json"
_MAX_SCHEMA_BYTES = 16 * 1024 * 1024

Downloader = Callable[[str], bytes]
CancellationCheck = Callable[[], bool]


class SchemaCatalogError(RuntimeError):
    """Stable fail-closed schema-catalog error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class KindParts:
    authority: str
    source: str
    entity: str
    version: str


def parse_kind(kind: str) -> KindParts:
    """Parse an exact four-part OSDU kind with a semantic schema version."""

    match = _KIND_PATTERN.fullmatch(kind)
    if match is None:
        raise SchemaCatalogError("SCHEMA_UNAVAILABLE", "The OSDU kind is invalid.")
    return KindParts(**match.groupdict())


def schema_relative_path(kind: str) -> str:
    """Map a supported exact OSDU kind to its catalog-relative wrapper path."""

    parsed = parse_kind(kind)
    entity = parsed.entity
    if entity == "Manifest" or entity.startswith("Generic"):
        group, name = "manifest", entity
    elif "--" in entity:
        group, name = entity.split("--", 1)
        if name.startswith("Generic"):
            group = "manifest"
    elif entity.startswith("Abstract"):
        group, name = "abstract", entity
    else:
        raise SchemaCatalogError(
            "SCHEMA_UNAVAILABLE",
            "The OSDU kind cannot be mapped to the pinned catalog.",
        )
    return f"{group}/{name}.{parsed.version}.json"


def walk_references(value: object) -> Iterable[str]:
    """Yield non-local JSON Schema references in deterministic document order."""

    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#"):
            yield reference
        for child in value.values():
            yield from walk_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_references(child)


def replace_schema_authority(value: Any, authority: str) -> Any:
    """Return a deep replacement of the OSDU schema-authority placeholder."""

    return _replace_placeholder(value, "{{schema-authority}}", authority)


def replace_namespace_placeholders(value: Any, authority: str = "osdu") -> Any:
    """Return a deep replacement of the legacy namespace placeholder."""

    return _replace_placeholder(value, "{{NAMESPACE}}", authority)


def _replace_placeholder(value: Any, marker: str, replacement: str) -> Any:
    if isinstance(value, str):
        return value.replace(marker, replacement)
    if isinstance(value, dict):
        return {
            key: _replace_placeholder(child, marker, replacement) for key, child in value.items()
        }
    if isinstance(value, list):
        return [_replace_placeholder(child, marker, replacement) for child in value]
    return value


class LoadedSchemaCatalog:
    """Checksum-verified immutable catalog used by TOOL-020."""

    def __init__(
        self,
        root: Path,
        descriptor: SchemaCatalogRef,
        checksums: dict[str, str],
    ) -> None:
        self.root = root
        self.descriptor = descriptor
        self._checksums = checksums
        self._schemas: dict[str, dict[str, Any]] = {}

    def load(self, kind: str) -> dict[str, Any]:
        existing = self._schemas.get(kind)
        if existing is not None:
            return existing
        relative = schema_relative_path(kind)
        if relative not in self._checksums:
            raise SchemaCatalogError(
                "SCHEMA_REFERENCE_FAILED", "A referenced schema is absent from the pinned catalog."
            )
        wrapper = _read_verified_wrapper(
            self.root / Path(relative),
            self._checksums[relative],
            missing_code="SCHEMA_REFERENCE_FAILED",
        )
        parsed = parse_kind(kind)
        schema = cast(
            dict[str, Any],
            replace_namespace_placeholders(
                replace_schema_authority(wrapper["schema"], parsed.authority),
                parsed.authority,
            ),
        )
        self._schemas[kind] = schema
        schema_id = schema.get("$id")
        if isinstance(schema_id, str):
            self._schemas[schema_id] = schema
        for reference in dict.fromkeys(walk_references(schema)):
            self.load(reference)
        return schema

    def registry(self) -> Registry[dict[str, Any]]:
        resources: list[tuple[str, Resource[dict[str, Any]]]] = []
        seen: set[int] = set()
        for uri, schema in self._schemas.items():
            identity = id(schema)
            if identity in seen:
                continue
            seen.add(identity)
            resources.append((uri, Resource.from_contents(schema, default_specification=DRAFT7)))
        return Registry().with_resources(resources)


class SchemaCatalogStore:
    """Install, activate, reopen, and verify versioned schema catalogs."""

    def __init__(
        self,
        cache_root: Path,
        *,
        network_policy: NetworkAccessPolicy | None = None,
        downloader: Downloader | None = None,
    ) -> None:
        self._root = cache_root
        self._network = network_policy or NetworkPolicy(enabled=False)
        self._downloader = downloader or _download_https

    def import_local(
        self,
        request: LocalSchemaCatalogImport,
        *,
        cancellation: CancellationCheck | None = None,
    ) -> SchemaCatalogRef:
        _check_cancelled(cancellation)
        source_root = Path(request.local_root.root)
        if source_root.is_symlink() or not source_root.is_dir():
            raise SchemaCatalogError(
                "CATALOG_INCOMPLETE", "The approved local schema export is unavailable."
            )

        def read(checksum: SchemaChecksum) -> bytes:
            candidate = source_root.joinpath(*checksum.relative_path.root.split("/"))
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(source_root.resolve(strict=True))
            except (OSError, ValueError) as error:
                raise SchemaCatalogError(
                    "CATALOG_INCOMPLETE", "A schema file is outside the approved local export."
                ) from error
            relative_parts = candidate.relative_to(source_root).parts
            inspected = [source_root]
            current = source_root
            for part in relative_parts:
                current /= part
                inspected.append(current)
            if any(path.is_symlink() for path in inspected):
                raise SchemaCatalogError(
                    "CATALOG_INCOMPLETE", "Schema catalog links are not imported."
                )
            try:
                return resolved.read_bytes()
            except OSError as error:
                raise SchemaCatalogError(
                    "CATALOG_INCOMPLETE", "A required schema file could not be read."
                ) from error

        return self._install(
            revision=request.revision,
            source=f"local_export:{source_root}",
            expected=request.expected_checksums,
            reader=read,
            cancellation=cancellation,
        )

    def refresh_remote(
        self,
        request: ApprovedRemoteSchemaCatalogRefresh,
        *,
        approval: NetworkApproval | None = None,
        now: Any = None,
        cancellation: CancellationCheck | None = None,
    ) -> SchemaCatalogRef:
        _check_cancelled(cancellation)
        parsed = urlparse(request.remote_uri)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SchemaCatalogError("NETWORK_NOT_APPROVED", "The remote schema URI is invalid.")
        if approval is None or approval.approval_id != request.network_approval_id:
            raise SchemaCatalogError(
                "NETWORK_NOT_APPROVED", "Remote schema refresh requires matching approval."
            )
        try:
            self._network.authorize(
                NetworkRequest(
                    purpose=NetworkPurpose.SCHEMA_CATALOG_REFRESH,
                    host=parsed.hostname,
                ),
                approval=approval,
                now=now,
            )
        except PolicyViolation as error:
            raise SchemaCatalogError(
                "NETWORK_NOT_APPROVED", "Remote schema refresh was not approved."
            ) from error

        base = request.remote_uri.rstrip("/")

        def read(checksum: SchemaChecksum) -> bytes:
            revision = quote(request.revision, safe="")
            relative = "/".join(
                quote(part, safe="._-") for part in checksum.relative_path.root.split("/")
            )
            url = f"{base}/{revision}/{relative}"
            try:
                payload = self._downloader(url)
            except Exception as error:
                raise SchemaCatalogError(
                    "CATALOG_INCOMPLETE", "A required remote schema could not be retrieved."
                ) from error
            if len(payload) > _MAX_SCHEMA_BYTES:
                raise SchemaCatalogError(
                    "CATALOG_INCOMPLETE", "A remote schema exceeds the configured bound."
                )
            return payload

        return self._install(
            revision=request.revision,
            source=f"approved_remote:{request.remote_uri}",
            expected=request.expected_checksums,
            reader=read,
            cancellation=cancellation,
        )

    def _install(
        self,
        *,
        revision: str,
        source: str,
        expected: tuple[SchemaChecksum, ...],
        reader: Callable[[SchemaChecksum], bytes],
        cancellation: CancellationCheck | None,
    ) -> SchemaCatalogRef:
        if not _REVISION_PATTERN.fullmatch(revision):
            raise SchemaCatalogError("CATALOG_INCOMPLETE", "The catalog revision is invalid.")
        paths = [item.relative_path.root for item in expected]
        if len(paths) != len(set(paths)):
            raise SchemaCatalogError("CATALOG_INCOMPLETE", "Catalog paths must be unique.")
        checksum_rows = tuple(sorted((item.relative_path.root, item.sha256) for item in expected))
        catalog_sha256 = _catalog_digest(revision, source, checksum_rows)
        catalog_id = uuid5(_CATALOG_NAMESPACE, catalog_sha256)
        destination = self._catalog_directory(revision, catalog_sha256)
        if destination.exists():
            descriptor = self.open(catalog_id).descriptor
            _check_cancelled(cancellation)
            self.activate(catalog_id)
            return descriptor.model_copy(
                update={"active": True, "activated_at": self.active_catalog().activated_at}
            )

        self._root.mkdir(parents=True, exist_ok=True)
        temporary = self._root / f".{catalog_id}.{uuid4().hex}.tmp"
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir()
        try:
            for checksum in expected:
                _check_cancelled(cancellation)
                payload = reader(checksum)
                if sha256(payload).hexdigest() != checksum.sha256:
                    raise SchemaCatalogError(
                        "CHECKSUM_MISMATCH", "A schema does not match its expected checksum."
                    )
                _validate_wrapper(payload)
                target = temporary.joinpath(*checksum.relative_path.root.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            manifest = {
                "schema_catalog_id": str(catalog_id),
                "revision": revision,
                "source": source,
                "catalog_sha256": catalog_sha256,
                "checksums": [
                    {"relative_path": path, "sha256": digest} for path, digest in checksum_rows
                ],
            }
            (temporary / _CATALOG_MANIFEST).write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            try:
                temporary.rename(destination)
            except FileExistsError:
                shutil.rmtree(temporary)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        _check_cancelled(cancellation)
        self.activate(catalog_id)
        return self.active_catalog()

    def open(self, catalog_id: UUID) -> LoadedSchemaCatalog:
        for manifest_path in self._root.glob(f"*/{_CATALOG_MANIFEST}"):
            manifest = _read_catalog_manifest(manifest_path)
            if manifest["schema_catalog_id"] != str(catalog_id):
                continue
            checksums = {item["relative_path"]: item["sha256"] for item in manifest["checksums"]}
            expected_catalog_hash = _catalog_digest(
                manifest["revision"],
                manifest["source"],
                tuple(sorted(checksums.items())),
            )
            expected_id = uuid5(_CATALOG_NAMESPACE, expected_catalog_hash)
            expected_directory = self._catalog_directory(
                manifest["revision"], expected_catalog_hash
            )
            if (
                expected_catalog_hash != manifest["catalog_sha256"]
                or expected_id != catalog_id
                or manifest_path.parent != expected_directory
            ):
                raise SchemaCatalogError(
                    "CHECKSUM_MISMATCH", "The schema catalog manifest is corrupt."
                )
            for relative, digest in checksums.items():
                _read_verified_wrapper(
                    manifest_path.parent / Path(relative),
                    digest,
                    missing_code="SCHEMA_UNAVAILABLE",
                )
            active = self._active_id() == catalog_id
            activated_at = _active_timestamp(self._root / _ACTIVE_MANIFEST) if active else None
            descriptor = SchemaCatalogRef(
                schema_catalog_id=catalog_id,
                revision=manifest["revision"],
                source=manifest["source"],
                catalog_sha256=manifest["catalog_sha256"],
                active=active,
                activated_at=activated_at,
            )
            return LoadedSchemaCatalog(manifest_path.parent, descriptor, checksums)
        raise SchemaCatalogError(
            "SCHEMA_UNAVAILABLE", "The requested schema catalog is unavailable."
        )

    def activate(self, catalog_id: UUID) -> SchemaCatalogRef:
        loaded = self.open(catalog_id)
        from datetime import UTC, datetime

        activated_at = datetime.now(UTC)
        payload = {
            "schema_catalog_id": str(catalog_id),
            "activated_at": activated_at.isoformat(),
        }
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = self._root / f".{_ACTIVE_MANIFEST}.{os.getpid()}.{uuid4().hex}.tmp"
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(self._root / _ACTIVE_MANIFEST)
        return loaded.descriptor.model_copy(update={"active": True, "activated_at": activated_at})

    def active_catalog(self) -> SchemaCatalogRef:
        catalog_id = self._active_id()
        if catalog_id is None:
            raise SchemaCatalogError("SCHEMA_UNAVAILABLE", "No schema catalog is active.")
        return self.open(catalog_id).descriptor

    def list_catalogs(self) -> tuple[SchemaCatalogRef, ...]:
        descriptors: list[SchemaCatalogRef] = []
        for manifest_path in sorted(self._root.glob(f"*/{_CATALOG_MANIFEST}")):
            manifest = _read_catalog_manifest(manifest_path)
            descriptors.append(self.open(UUID(manifest["schema_catalog_id"])).descriptor)
        return tuple(descriptors)

    def catalog_path(self, catalog_id: UUID) -> Path:
        return self.open(catalog_id).root

    def _active_id(self) -> UUID | None:
        path = self._root / _ACTIVE_MANIFEST
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return UUID(value["schema_catalog_id"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise SchemaCatalogError(
                "SCHEMA_UNAVAILABLE", "The active schema catalog pointer is corrupt."
            ) from error

    def _catalog_directory(self, revision: str, digest: str) -> Path:
        return self._root / f"{revision}-{digest[:16]}"


def _catalog_digest(
    revision: str,
    source: str,
    checksums: tuple[tuple[str, str], ...],
) -> str:
    payload = {"revision": revision, "source": source, "checksums": checksums}
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _validate_wrapper(payload: bytes) -> dict[str, Any]:
    if len(payload) > _MAX_SCHEMA_BYTES:
        raise SchemaCatalogError("CATALOG_INCOMPLETE", "A schema exceeds the configured bound.")
    try:
        wrapper = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SchemaCatalogError(
            "CATALOG_INCOMPLETE", "A schema wrapper is invalid JSON."
        ) from error
    if not isinstance(wrapper, dict) or not isinstance(wrapper.get("schema"), dict):
        raise SchemaCatalogError("CATALOG_INCOMPLETE", "A schema wrapper is invalid.")
    return wrapper


def _read_verified_wrapper(path: Path, expected: str, *, missing_code: str) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise SchemaCatalogError(missing_code, "A required schema file is unavailable.") from error
    if sha256(payload).hexdigest() != expected:
        raise SchemaCatalogError("CHECKSUM_MISMATCH", "A cached schema is corrupt.")
    return _validate_wrapper(payload)


def _read_catalog_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        UUID(value["schema_catalog_id"])
        if (
            not isinstance(value["revision"], str)
            or not isinstance(value["source"], str)
            or not isinstance(value["catalog_sha256"], str)
            or not isinstance(value["checksums"], list)
        ):
            raise TypeError
        for item in value["checksums"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("relative_path"), str)
                or not isinstance(item.get("sha256"), str)
            ):
                raise TypeError
        return cast(dict[str, Any], value)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SchemaCatalogError(
            "SCHEMA_UNAVAILABLE", "A schema catalog manifest is corrupt."
        ) from error


def _active_timestamp(path: Path) -> Any:
    from datetime import datetime

    try:
        return datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["activated_at"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SchemaCatalogError(
            "SCHEMA_UNAVAILABLE", "The active schema catalog pointer is corrupt."
        ) from error


def _download_https(url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Only HTTPS schema sources are supported.")
    request = Request(url, headers={"User-Agent": "AgenticOSDUDataPreparation/1"})
    with urlopen(request, timeout=30) as response:  # nosec B310 - HTTPS checked above
        final = urlparse(response.geturl())
        if final.scheme != "https" or final.hostname != parsed.hostname:
            raise ValueError("Schema redirects must remain on the approved HTTPS host.")
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > _MAX_SCHEMA_BYTES:
            raise ValueError("Schema response exceeds the configured bound.")
        payload = cast(bytes, response.read(_MAX_SCHEMA_BYTES + 1))
    if len(payload) > _MAX_SCHEMA_BYTES:
        raise ValueError("Schema response exceeds the configured bound.")
    return payload


def _check_cancelled(cancellation: CancellationCheck | None) -> None:
    if cancellation is not None and cancellation():
        raise SchemaCatalogError("CANCELLED", "Schema catalog processing was cancelled.")


__all__ = [
    "KindParts",
    "LoadedSchemaCatalog",
    "SchemaCatalogError",
    "SchemaCatalogStore",
    "parse_kind",
    "replace_namespace_placeholders",
    "replace_schema_authority",
    "schema_relative_path",
    "walk_references",
]
