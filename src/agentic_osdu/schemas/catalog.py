"""TOOL-021 immutable, checksum-pinned OSDU schema catalogs."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from errno import ELOOP
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
_LOCK_FILE = ".catalog.lock"
_MAX_SCHEMA_BYTES = 16 * 1024 * 1024
_CATALOG_THREAD_LOCK = threading.RLock()

Downloader = Callable[[str], bytes]
CancellationCheck = Callable[[], bool]


@contextmanager
def _catalog_write_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with _CATALOG_THREAD_LOCK, (root / _LOCK_FILE).open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


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


@dataclass(frozen=True, slots=True)
class _FilesystemIdentity:
    device: int
    inode: int
    file_type: int


@dataclass(slots=True)
class LocalSchemaReadCapability:
    """Filesystem-identity-bound read capability for one approved catalog root."""

    root: Path
    identity: _FilesystemIdentity
    physical_root: Path
    _observed_directories: dict[Path, tuple[_FilesystemIdentity, Path]] = field(
        default_factory=dict
    )
    _observed_files: dict[Path, _FilesystemIdentity] = field(default_factory=dict)
    _descriptors: list[int] = field(default_factory=list)
    _anchor: LocalSchemaReadCapability | None = None

    @property
    def identity_tuple(self) -> tuple[int, int, int]:
        return (self.identity.device, self.identity.inode, self.identity.file_type)

    @classmethod
    def capture(
        cls,
        root: Path,
        *,
        error_code: str = "CATALOG_INCOMPLETE",
        anchor: LocalSchemaReadCapability | None = None,
    ) -> LocalSchemaReadCapability:
        canonical = Path(os.path.abspath(root))
        if anchor is None:
            traversal_root = Path(canonical.anchor)
            descriptor, _, physical_traversal_root = _open_bound_descriptor(
                traversal_root,
                require_directory=True,
                error_code=error_code,
            )
            descriptors = [descriptor]
            observed_locations: dict[Path, tuple[_FilesystemIdentity, Path]] = {}
            current = traversal_root
            expected_physical = physical_traversal_root
            try:
                for part in canonical.relative_to(traversal_root).parts:
                    current /= part
                    expected_physical /= part
                    descriptor, identity, physical_path = _open_bound_descriptor(
                        current,
                        require_directory=True,
                        error_code=error_code,
                    )
                    descriptors.append(descriptor)
                    if _normalize_physical_path(str(physical_path)) != _normalize_physical_path(
                        str(expected_physical)
                    ):
                        raise SchemaCatalogError(
                            "ROOT_POLICY_DENIED",
                            "The local schema path traversed a link or reparse point.",
                        )
                    observed_locations[current] = (identity, physical_path)
                if observed_locations:
                    identity = observed_locations[canonical][0]
                else:
                    identity = _identity_from_metadata(os.fstat(descriptors[0]))
                capability = cls(
                    root=canonical,
                    identity=identity,
                    physical_root=_normalize_physical_path(str(expected_physical)),
                    _observed_directories=observed_locations,
                    _descriptors=descriptors,
                )
                capability.validate()
                return capability
            except Exception:
                for descriptor in descriptors:
                    os.close(descriptor)
                raise
        else:
            anchor.validate()
            try:
                relative = canonical.relative_to(anchor.root)
            except ValueError as error:
                raise SchemaCatalogError(
                    "ROOT_POLICY_DENIED",
                    "The local schema path is outside the approved workspace root.",
                ) from error
            current = anchor.root
            expected_physical = anchor.physical_root
            observed_locations = {}
            descriptors = []
            try:
                for part in relative.parts:
                    current /= part
                    expected_physical /= part
                    descriptor, identity, physical_path = _open_bound_descriptor(
                        current,
                        require_directory=True,
                        error_code=error_code,
                    )
                    descriptors.append(descriptor)
                    if _normalize_physical_path(str(physical_path)) != _normalize_physical_path(
                        str(expected_physical)
                    ):
                        raise SchemaCatalogError(
                            "ROOT_POLICY_DENIED",
                            "The local schema path traversed a link or reparse point.",
                        )
                    observed_locations[current] = (identity, physical_path)
                    anchor.validate()
                if not descriptors:
                    descriptor, identity, physical_root = _open_bound_descriptor(
                        canonical,
                        require_directory=True,
                        error_code=error_code,
                    )
                    descriptors.append(descriptor)
                else:
                    identity = observed_locations[canonical][0]
                    physical_root = _normalize_physical_path(str(expected_physical))
                capability = cls(
                    root=canonical,
                    identity=identity,
                    physical_root=physical_root,
                    _observed_directories=observed_locations,
                    _descriptors=descriptors,
                    _anchor=anchor,
                )
                capability.validate()
                return capability
            except Exception:
                for descriptor in descriptors:
                    os.close(descriptor)
                raise

    def validate(self) -> None:
        if self._anchor is not None:
            self._anchor.validate()
        _require_same_identity_and_location(
            self.root,
            self.identity,
            self.physical_root,
            require_directory=True,
        )
        for path, (identity, physical_path) in self._observed_directories.items():
            _require_same_identity_and_exact_location(
                path,
                identity,
                physical_path,
                require_directory=True,
            )
        for path, identity in self._observed_files.items():
            _require_same_identity_and_location(
                path,
                identity,
                self.physical_root,
                require_file=True,
            )

    def observe_directory(self, path: Path) -> None:
        descriptor, identity, physical_path = _open_bound_descriptor(
            path,
            require_directory=True,
            error_code="CATALOG_INCOMPLETE",
        )
        try:
            _require_physical_containment(self.physical_root, physical_path)
            previous = self._observed_directories.setdefault(path, (identity, physical_path))
            if previous[0] != identity or previous[1] != physical_path:
                raise SchemaCatalogError(
                    "ROOT_POLICY_DENIED",
                    "The approved local schema path changed during import.",
                )
            if previous[0] == identity and not any(
                _identity_from_metadata(os.fstat(item)) == identity for item in self._descriptors
            ):
                self._descriptors.append(descriptor)
                descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def observe_file(
        self,
        path: Path,
        identity: _FilesystemIdentity,
        descriptor: int,
    ) -> None:
        previous = self._observed_files.setdefault(path, identity)
        if previous != identity:
            raise SchemaCatalogError(
                "ROOT_POLICY_DENIED",
                "The approved local schema file changed during import.",
            )
        if previous == identity and not any(
            _identity_from_metadata(os.fstat(item)) == identity for item in self._descriptors
        ):
            self._descriptors.append(os.dup(descriptor))

    def close(self) -> None:
        for descriptor in reversed(self._descriptors):
            os.close(descriptor)
        self._descriptors.clear()
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None


def _identity_from_metadata(metadata: os.stat_result) -> _FilesystemIdentity:
    return _FilesystemIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        file_type=stat.S_IFMT(metadata.st_mode),
    )


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _normalize_physical_path(path: str) -> Path:
    if path.startswith("\\\\?\\UNC\\"):
        path = f"\\\\{path[8:]}"
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return Path(os.path.normcase(os.path.normpath(path)))


def _physical_path_from_descriptor(descriptor: int, fallback: Path) -> Path:
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        get_final_path = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        get_final_path.restype = wintypes.DWORD
        handle = msvcrt.get_osfhandle(descriptor)
        size = get_final_path(handle, None, 0, 0)
        if size == 0:
            raise OSError("path handle resolution failed")
        buffer = ctypes.create_unicode_buffer(size + 1)
        if get_final_path(handle, buffer, len(buffer), 0) == 0:
            raise OSError("path handle resolution failed")
        return _normalize_physical_path(buffer.value)
    for prefix in ("/proc/self/fd", "/dev/fd"):
        link = Path(prefix) / str(descriptor)
        try:
            return _normalize_physical_path(os.readlink(link))
        except OSError:
            continue
    return _normalize_physical_path(str(fallback.resolve(strict=True)))


def _open_windows_descriptor(path: Path, *, require_directory: bool) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    access = 0x80 if require_directory else 0x80000000
    flags = 0x00200000 | (0x02000000 if require_directory else 0)
    handle = create_file(str(path), access, 0x3, None, 3, flags, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "path handle open failed")
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except Exception:
        ctypes.windll.kernel32.CloseHandle(handle)
        raise


def _open_bound_descriptor(
    path: Path,
    *,
    require_directory: bool = False,
    require_file: bool = False,
    error_code: str = "ROOT_POLICY_DENIED",
) -> tuple[int, _FilesystemIdentity, Path]:
    descriptor = -1
    try:
        if os.name == "nt":
            descriptor = _open_windows_descriptor(path, require_directory=require_directory)
        else:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            if require_directory:
                flags |= getattr(os, "O_DIRECTORY", 0)
            descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        physical_path = _physical_path_from_descriptor(descriptor, path)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        denied = error.errno == ELOOP or getattr(error, "winerror", None) in {
            681,
            1920,
            4392,
            4393,
            4394,
        }
        raise SchemaCatalogError(
            "ROOT_POLICY_DENIED" if denied else error_code,
            "The approved local schema path is unavailable.",
        ) from error
    if _is_link_or_reparse(metadata):
        os.close(descriptor)
        raise SchemaCatalogError(
            "ROOT_POLICY_DENIED",
            "Links and reparse points are not allowed in local schema paths.",
        )
    if require_directory and not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise SchemaCatalogError(error_code, "A required local schema directory is unavailable.")
    if require_file and not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise SchemaCatalogError(error_code, "A required local schema file is unavailable.")
    return descriptor, _identity_from_metadata(metadata), physical_path


def _require_physical_containment(root: Path, candidate: Path) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise SchemaCatalogError(
            "ROOT_POLICY_DENIED",
            "The approved local schema path resolved outside its physical root.",
        ) from error


def _require_same_identity_and_location(
    path: Path,
    expected: _FilesystemIdentity,
    physical_root: Path,
    *,
    require_directory: bool = False,
    require_file: bool = False,
) -> None:
    descriptor, current, physical_path = _open_bound_descriptor(
        path,
        require_directory=require_directory,
        require_file=require_file,
    )
    os.close(descriptor)
    _require_physical_containment(physical_root, physical_path)
    if current != expected:
        raise SchemaCatalogError(
            "ROOT_POLICY_DENIED",
            "The approved local schema path changed during import.",
        )


def _require_same_identity_and_exact_location(
    path: Path,
    expected: _FilesystemIdentity,
    expected_physical_path: Path,
    *,
    require_directory: bool = False,
) -> None:
    descriptor, current, physical_path = _open_bound_descriptor(
        path,
        require_directory=require_directory,
    )
    os.close(descriptor)
    if current != expected or _normalize_physical_path(
        str(physical_path)
    ) != _normalize_physical_path(str(expected_physical_path)):
        raise SchemaCatalogError(
            "ROOT_POLICY_DENIED",
            "The approved local schema path changed during import.",
        )


def _read_bound_file(
    capability: LocalSchemaReadCapability,
    relative_path: str,
) -> bytes:
    capability.validate()
    parts = relative_path.split("/")
    current = capability.root
    for part in parts[:-1]:
        current /= part
        capability.observe_directory(current)
    candidate = current / parts[-1]
    descriptor, file_identity, physical_path = _open_bound_descriptor(
        candidate,
        require_file=True,
        error_code="CATALOG_INCOMPLETE",
    )
    try:
        _require_physical_containment(capability.physical_root, physical_path)
        capability.observe_file(candidate, file_identity, descriptor)
        capability.validate()
        chunks: list[bytes] = []
        size = 0
        while True:
            capability.validate()
            if _identity_from_metadata(os.fstat(descriptor)) != file_identity:
                raise SchemaCatalogError(
                    "ROOT_POLICY_DENIED",
                    "A schema file changed while it was being read.",
                )
            chunk = os.read(descriptor, min(1024 * 1024, _MAX_SCHEMA_BYTES + 1 - size))
            capability.validate()
            if _identity_from_metadata(os.fstat(descriptor)) != file_identity:
                raise SchemaCatalogError(
                    "ROOT_POLICY_DENIED",
                    "A schema file changed while it was being read.",
                )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_SCHEMA_BYTES:
                raise SchemaCatalogError(
                    "CATALOG_INCOMPLETE",
                    "A local schema exceeds the configured bound.",
                )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


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
        capability: LocalSchemaReadCapability | None = None,
    ) -> SchemaCatalogRef:
        source_root = Path(os.path.abspath(request.local_root.root))
        access = capability or LocalSchemaReadCapability.capture(source_root)
        try:
            _check_cancelled(cancellation)
            if os.path.normcase(str(access.root)) != os.path.normcase(str(source_root)):
                raise SchemaCatalogError(
                    "ROOT_POLICY_DENIED",
                    "The local schema capability does not match the approved root.",
                )
            access.validate()

            def read(checksum: SchemaChecksum) -> bytes:
                return _read_bound_file(access, checksum.relative_path.root)

            return self._install(
                revision=request.revision,
                source=f"local_export:{source_root}",
                expected=request.expected_checksums,
                reader=read,
                cancellation=cancellation,
                before_publish=access.validate,
            )
        finally:
            access.close()

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
        before_publish: Callable[[], None] | None = None,
    ) -> SchemaCatalogRef:
        with _catalog_write_lock(self._root):
            return self._install_locked(
                revision=revision,
                source=source,
                expected=expected,
                reader=reader,
                cancellation=cancellation,
                before_publish=before_publish,
            )

    def _install_locked(
        self,
        *,
        revision: str,
        source: str,
        expected: tuple[SchemaChecksum, ...],
        reader: Callable[[SchemaChecksum], bytes],
        cancellation: CancellationCheck | None,
        before_publish: Callable[[], None] | None,
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
        previous_active_id = self._active_id()

        def verify_source() -> None:
            for checksum in expected:
                _check_cancelled(cancellation)
                payload = reader(checksum)
                if sha256(payload).hexdigest() != checksum.sha256:
                    raise SchemaCatalogError(
                        "CHECKSUM_MISMATCH", "A schema does not match its expected checksum."
                    )
                _validate_wrapper(payload)

        def restore_active() -> None:
            if previous_active_id is None:
                (self._root / _ACTIVE_MANIFEST).unlink(missing_ok=True)
            else:
                self._activate_unlocked(previous_active_id)

        if destination.exists():
            descriptor = self.open(catalog_id).descriptor
            activated = False
            try:
                verify_source()
                if before_publish is not None:
                    before_publish()
                self._activate_unlocked(catalog_id)
                activated = True
                if before_publish is not None:
                    before_publish()
                return descriptor.model_copy(
                    update={"active": True, "activated_at": self.active_catalog().activated_at}
                )
            except Exception:
                if activated:
                    restore_active()
                raise

        self._root.mkdir(parents=True, exist_ok=True)
        temporary = self._root / f".{catalog_id}.{uuid4().hex}.tmp"
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir()
        published = False
        activated = False
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
            if before_publish is not None:
                before_publish()
            try:
                temporary.rename(destination)
                published = True
            except FileExistsError:
                shutil.rmtree(temporary)
            _check_cancelled(cancellation)
            if before_publish is not None:
                before_publish()
            self._activate_unlocked(catalog_id)
            activated = True
            if before_publish is not None:
                before_publish()
            return self.active_catalog()
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            if activated:
                restore_active()
            if published:
                shutil.rmtree(destination, ignore_errors=True)
            raise

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
        with _catalog_write_lock(self._root):
            return self._activate_unlocked(catalog_id)

    def _activate_unlocked(self, catalog_id: UUID) -> SchemaCatalogRef:
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
