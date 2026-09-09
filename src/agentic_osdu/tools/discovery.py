"""Approved workspace registration, deterministic discovery, and bounded reads."""

from __future__ import annotations

import base64
import fnmatch
import json
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from threading import Event, Lock
from typing import BinaryIO, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from agentic_osdu.domain.models import FileAssetRef, FileSampleRef, WorkspaceRelativePath
from agentic_osdu.policy import (
    PathStyle,
    PolicyViolation,
    WindowsAwarePathPolicy,
)
from agentic_osdu.tools.contracts import (
    DiscoverFilesInput,
    DiscoveryBatch,
    DiscoveryOutput,
    EncodingPolicy,
    FileSampleOutput,
    ReadFileSampleInput,
    RegisterWorkspaceInput,
    SampleMode,
    WorkspaceDescriptor,
)

DISCOVERY_VERSION = 1


class DiscoveryError(RuntimeError):
    """Stable, path-safe TOOL-001..003 failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class WorkspacePolicyStore(Protocol):
    """Persistence boundary for approved workspace policies."""

    def save(self, descriptor: WorkspaceDescriptor) -> WorkspaceDescriptor: ...

    def get(self, workspace_id: UUID) -> WorkspaceDescriptor | None: ...


class InMemoryWorkspacePolicyStore:
    """Thread-safe policy store suitable for local composition and tests."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, WorkspaceDescriptor] = {}
        self._by_fingerprint: dict[str, WorkspaceDescriptor] = {}
        self._lock = Lock()

    def save(self, descriptor: WorkspaceDescriptor) -> WorkspaceDescriptor:
        with self._lock:
            existing = self._by_fingerprint.get(descriptor.policy_fingerprint)
            if existing is not None:
                return existing
            self._by_id[descriptor.workspace_id] = descriptor
            self._by_fingerprint[descriptor.policy_fingerprint] = descriptor
            return descriptor

    def get(self, workspace_id: UUID) -> WorkspaceDescriptor | None:
        with self._lock:
            return self._by_id.get(workspace_id)


class CancellationToken:
    """Cooperative cancellation checked between filesystem operations."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True, slots=True)
class DiscoveryProgress:
    event: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class _FileState:
    asset: FileAssetRef
    canonical_path: str
    signature: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class DiscoveredFormatSource:
    """Read-only format-parser capability backed by a discovered fingerprint."""

    file_id: UUID
    _service: DiscoveryService

    @property
    def size_bytes(self) -> int:
        return self._service._validated_file_state(self.file_id).asset.size_bytes

    @property
    def relative_path(self) -> WorkspaceRelativePath:
        return self._service._validated_file_state(self.file_id).asset.relative_path

    def open_binary(self, *, max_bytes: int | None = None) -> AbstractContextManager[BinaryIO]:
        return self._service._open_discovered_binary(self.file_id, max_bytes=max_bytes)

    def open_native(self) -> AbstractContextManager[object]:
        return self._service._open_discovered_native(self.file_id)


class DiscoveryService:
    """Deterministic implementation boundary for TOOL-001 through TOOL-003."""

    def __init__(
        self,
        *,
        store: WorkspacePolicyStore,
        path_policy: WindowsAwarePathPolicy | None = None,
        progress_batch_size: int = 100,
    ) -> None:
        if progress_batch_size < 1:
            raise ValueError("progress_batch_size must be positive")
        self._store = store
        self._path_policy = path_policy or WindowsAwarePathPolicy(
            style=PathStyle.WINDOWS if os.name == "nt" else PathStyle.POSIX
        )
        self._progress_batch_size = progress_batch_size
        self._files: dict[UUID, _FileState] = {}
        self._lock = Lock()

    def register_workspace(self, request: RegisterWorkspaceInput) -> WorkspaceDescriptor:
        root = Path(request.root_path.root)
        try:
            if not root.exists():
                raise DiscoveryError("ROOT_NOT_FOUND", "The approved workspace root was not found.")
            if not root.is_dir():
                raise DiscoveryError(
                    "ROOT_NOT_DIRECTORY", "The approved workspace root is not a directory."
                )
            canonical = str(root.resolve(strict=True))
            if self._path_policy.inspector.is_link_or_reparse(str(root)):
                raise DiscoveryError(
                    "ROOT_POLICY_DENIED", "Linked or reparse-point workspace roots are denied."
                )
            canonical = self._path_policy.approve_root(canonical)
        except DiscoveryError:
            raise
        except (OSError, PolicyViolation) as error:
            raise DiscoveryError(
                "ROOT_POLICY_DENIED", "The workspace root could not be approved safely."
            ) from error

        outputs = sorted(path.root for path in request.allowed_output_subpaths)
        policy_document = {
            "allowed_output_subpaths": outputs,
            "canonical_root": (
                canonical.casefold() if self._path_policy.style is PathStyle.WINDOWS else canonical
            ),
            "read_only": request.read_only,
            "version": "1.0.0",
        }
        fingerprint = sha256(
            json.dumps(policy_document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        descriptor = WorkspaceDescriptor(
            workspace_id=uuid5(NAMESPACE_URL, f"workspace-policy:{fingerprint}"),
            canonical_root=canonical,
            read_only=request.read_only,
            allowed_output_subpaths=tuple(WorkspaceRelativePath(path) for path in outputs),
            policy_fingerprint=fingerprint,
        )
        return self._store.save(descriptor)

    def discover_files(
        self,
        request: DiscoverFilesInput,
        *,
        cancellation: CancellationToken | None = None,
        on_progress: Callable[[DiscoveryProgress], None] | None = None,
    ) -> DiscoveryOutput:
        workspace = self._store.get(request.workspace_id)
        if workspace is None or workspace.workspace_id != request.workspace_id:
            raise DiscoveryError(
                "ROOT_POLICY_DENIED", "The workspace has no approved persisted policy."
            )
        if request.follow_symlinks:
            raise DiscoveryError(
                "ROOT_POLICY_DENIED", "Discovery does not follow links or reparse points."
            )
        self._check_cancelled(cancellation)
        self._emit(on_progress, "discovery.started", 0, 0)

        roots = self._include_roots(workspace, request.include_paths)
        assets: list[FileAssetRef] = []
        states: list[_FileState] = []
        total_bytes = 0
        for path in self._walk_ordered(roots, cancellation):
            relative = path.relative_to(Path(workspace.canonical_root.root)).as_posix()
            if self._excluded(relative, request.exclude_globs):
                continue
            try:
                authorized = self._path_policy.authorize_child(
                    workspace.canonical_root.root, relative, follow_links=False
                )
                metadata = path.stat(follow_symlinks=False)
            except (OSError, PolicyViolation) as error:
                raise DiscoveryError(
                    "IO_READ_FAILED", "A discovered path could not be inspected safely."
                ) from error
            if not path.is_file() or self._path_policy.inspector.is_link_or_reparse(
                authorized.canonical_path
            ):
                continue
            if len(assets) >= request.max_files:
                raise DiscoveryError(
                    "FILE_LIMIT_EXCEEDED", "Discovery exceeded the configured file limit."
                )
            if total_bytes + metadata.st_size > request.max_total_bytes:
                raise DiscoveryError(
                    "FILE_LIMIT_EXCEEDED", "Discovery exceeded the configured byte limit."
                )
            signature = self._signature(metadata)
            file_id = uuid5(
                workspace.workspace_id,
                f"{relative}\0{signature[0]}\0{signature[1]}\0{signature[2]}\0{signature[3]}",
            )
            asset = FileAssetRef(
                file_id=file_id,
                workspace_id=workspace.workspace_id,
                relative_path=WorkspaceRelativePath(relative),
                size_bytes=metadata.st_size,
                modified_at=datetime.fromtimestamp(metadata.st_mtime, UTC),
                discovery_version=DISCOVERY_VERSION,
            )
            assets.append(asset)
            states.append(
                _FileState(
                    asset=asset,
                    canonical_path=authorized.canonical_path,
                    signature=signature,
                )
            )
            total_bytes += metadata.st_size
            if len(assets) % self._progress_batch_size == 0:
                self._emit(on_progress, "discovery.batch", len(assets), total_bytes)
        if assets and len(assets) % self._progress_batch_size:
            self._emit(on_progress, "discovery.batch", len(assets), total_bytes)

        ordered = sorted(
            zip(assets, states, strict=True),
            key=lambda pair: (pair[0].relative_path.root.casefold(), pair[0].relative_path.root),
        )
        assets = [pair[0] for pair in ordered]
        states = [pair[1] for pair in ordered]
        snapshot_at = max(
            (item.modified_at for item in assets),
            default=datetime.fromtimestamp(
                Path(workspace.canonical_root.root).stat().st_mtime, UTC
            ),
        )
        discovery_key = "\n".join(str(item.file_id) for item in assets)
        batch = DiscoveryBatch(
            discovery_id=uuid5(
                workspace.workspace_id, f"discovery:{DISCOVERY_VERSION}:{discovery_key}"
            ),
            workspace_id=workspace.workspace_id,
            file_count=len(assets),
            total_bytes=total_bytes,
            snapshot_at=snapshot_at,
        )
        with self._lock:
            for state in states:
                self._files[state.asset.file_id] = state
        self._emit(on_progress, "discovery.completed", len(assets), total_bytes)
        return DiscoveryOutput(batch=batch, files=tuple(assets))

    def read_file_sample(self, request: ReadFileSampleInput) -> FileSampleOutput:
        with self._lock:
            state = self._files.get(request.file_id)
        if state is None:
            raise DiscoveryError("FILE_NOT_FOUND", "The file was not discovered.")
        workspace = self._store.get(state.asset.workspace_id)
        if workspace is None:
            raise DiscoveryError("ROOT_POLICY_DENIED", "The file workspace is no longer approved.")
        try:
            authorized = self._path_policy.authorize_child(
                workspace.canonical_root.root,
                state.asset.relative_path.root,
                follow_links=False,
            )
            before = os.stat(authorized.canonical_path, follow_symlinks=False)
        except (OSError, PolicyViolation) as error:
            raise DiscoveryError(
                "FILE_CHANGED", "The file cannot be matched to its discovery fingerprint."
            ) from error
        if self._signature(before) != state.signature:
            raise DiscoveryError("FILE_CHANGED", "The file changed after discovery.")

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(authorized.canonical_path, flags)
            opened = os.fstat(descriptor)
            if self._signature(opened) != state.signature:
                raise DiscoveryError("FILE_CHANGED", "The file changed after discovery.")
            offset = request.offset if request.mode is SampleMode.RANGE else 0
            os.lseek(descriptor, offset, os.SEEK_SET)
            content = os.read(descriptor, request.max_bytes)
            after = os.fstat(descriptor)
        except DiscoveryError:
            raise
        except OSError as error:
            raise DiscoveryError("IO_READ_FAILED", "The bounded file read failed.") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            current = os.stat(authorized.canonical_path, follow_symlinks=False)
        except OSError as error:
            raise DiscoveryError("FILE_CHANGED", "The file changed during sampling.") from error
        if self._signature(after) != state.signature or self._signature(current) != state.signature:
            raise DiscoveryError("FILE_CHANGED", "The file changed during sampling.")
        if not content:
            raise DiscoveryError("INSUFFICIENT_SAMPLE", "The requested range contained no bytes.")

        encoding = self._validate_or_detect_encoding(content, request.encoding_policy)
        digest = sha256(content).hexdigest()
        sample = FileSampleRef(
            sample_id=uuid5(
                request.file_id,
                f"{offset}:{len(content)}:{digest}:{encoding or 'binary'}",
            ),
            file_id=request.file_id,
            offset=offset,
            length=len(content),
            sha256=digest,
            encoding=encoding,
        )
        return FileSampleOutput(
            sample=sample,
            content_base64=base64.b64encode(content).decode("ascii"),
            text_encoding=encoding,
            truncated=offset + len(content) < state.asset.size_bytes,
        )

    def format_source(self, file_id: UUID) -> DiscoveredFormatSource:
        """Resolve a file ID to a read-only parser capability after fingerprint validation."""

        self._validated_file_state(file_id)
        return DiscoveredFormatSource(file_id=file_id, _service=self)

    def _validated_file_state(self, file_id: UUID) -> _FileState:
        with self._lock:
            state = self._files.get(file_id)
        if state is None:
            raise DiscoveryError("FILE_NOT_FOUND", "The file was not discovered.")
        workspace = self._store.get(state.asset.workspace_id)
        if workspace is None:
            raise DiscoveryError("ROOT_POLICY_DENIED", "The file workspace is no longer approved.")
        try:
            authorized = self._path_policy.authorize_child(
                workspace.canonical_root.root,
                state.asset.relative_path.root,
                follow_links=False,
            )
            current = os.stat(authorized.canonical_path, follow_symlinks=False)
        except (OSError, PolicyViolation) as error:
            raise DiscoveryError(
                "FILE_CHANGED", "The file cannot be matched to its discovery fingerprint."
            ) from error
        if (
            authorized.canonical_path != state.canonical_path
            or self._signature(current) != state.signature
        ):
            raise DiscoveryError("FILE_CHANGED", "The file changed after discovery.")
        return state

    @contextmanager
    def _open_discovered_binary(
        self,
        file_id: UUID,
        *,
        max_bytes: int | None,
    ) -> Iterator[BinaryIO]:
        state = self._validated_file_state(file_id)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(state.canonical_path, flags)
            opened = os.fstat(descriptor)
            if self._signature(opened) != state.signature:
                raise DiscoveryError("FILE_CHANGED", "The file changed before parser access.")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                if max_bytes is None:
                    yield stream
                else:
                    yield BytesIO(stream.read(max_bytes))
        except DiscoveryError:
            raise
        except OSError as error:
            raise DiscoveryError("IO_READ_FAILED", "The parser file read failed.") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._validated_file_state(file_id)

    @contextmanager
    def _open_discovered_native(self, file_id: UUID) -> Iterator[object]:
        """Hold the discovered inode while a native parser uses a supported locator."""

        state = self._validated_file_state(file_id)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(state.canonical_path, flags)
            if self._signature(os.fstat(descriptor)) != state.signature:
                raise DiscoveryError(
                    "FILE_CHANGED", "The file changed before native parser access."
                )
            if os.name == "nt":
                # Windows' default sharing mode prevents replacement while this handle is held.
                locator: object = state.canonical_path
            else:
                descriptor_paths = (
                    f"/proc/self/fd/{descriptor}",
                    f"/dev/fd/{descriptor}",
                )
                locator = next((path for path in descriptor_paths if os.path.exists(path)), None)
                if locator is None:
                    raise DiscoveryError(
                        "IO_READ_FAILED",
                        "This platform cannot provide a stable native-parser locator.",
                    )
            yield locator
            if self._signature(os.fstat(descriptor)) != state.signature:
                raise DiscoveryError(
                    "FILE_CHANGED", "The file changed during native parser access."
                )
        except DiscoveryError:
            raise
        except OSError as error:
            raise DiscoveryError("IO_READ_FAILED", "The native parser file open failed.") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._validated_file_state(file_id)

    def _include_roots(
        self,
        workspace: WorkspaceDescriptor,
        include_paths: tuple[WorkspaceRelativePath, ...],
    ) -> tuple[Path, ...]:
        if not include_paths:
            return (Path(workspace.canonical_root.root),)
        roots = []
        for include in include_paths:
            try:
                authorized = self._path_policy.authorize_child(
                    workspace.canonical_root.root,
                    include.root,
                    follow_links=False,
                )
            except PolicyViolation as error:
                raise DiscoveryError(
                    "PATH_OUTSIDE_ROOT", "An include path is outside the approved root."
                ) from error
            root = Path(authorized.canonical_path)
            if not root.exists():
                raise DiscoveryError("IO_READ_FAILED", "An include path was not found.")
            roots.append(root)
        return tuple(
            sorted(
                set(roots),
                key=lambda path: (
                    path.as_posix().casefold(),
                    path.as_posix(),
                ),
            )
        )

    def _walk_ordered(
        self,
        roots: tuple[Path, ...],
        cancellation: CancellationToken | None,
    ) -> Iterator[Path]:
        stack = list(reversed(roots))
        while stack:
            self._check_cancelled(cancellation)
            current = stack.pop()
            if current.is_file():
                yield current
                continue
            try:
                entries = sorted(
                    os.scandir(current),
                    key=lambda entry: (entry.name.casefold(), entry.name),
                )
            except OSError as error:
                raise DiscoveryError(
                    "IO_READ_FAILED", "A directory could not be read safely."
                ) from error
            directories: list[Path] = []
            for entry in entries:
                self._check_cancelled(cancellation)
                try:
                    if entry.is_symlink() or self._path_policy.inspector.is_link_or_reparse(
                        entry.path
                    ):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        yield Path(entry.path)
                except OSError as error:
                    raise DiscoveryError(
                        "IO_READ_FAILED", "A directory entry could not be inspected safely."
                    ) from error
            stack.extend(reversed(directories))

    @staticmethod
    def _excluded(relative: str, patterns: tuple[str, ...]) -> bool:
        path = PurePosixPath(relative)
        folded = relative.casefold()
        return any(
            path.match(pattern.replace("\\", "/"))
            or fnmatch.fnmatchcase(folded, pattern.replace("\\", "/").casefold())
            for pattern in patterns
        )

    @staticmethod
    def _signature(metadata: os.stat_result) -> tuple[int, int, int, int]:
        return (
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_dev,
            metadata.st_ino,
        )

    @staticmethod
    def _validate_or_detect_encoding(content: bytes, policy: EncodingPolicy) -> str | None:
        if policy is EncodingPolicy.BINARY:
            return None
        if policy is EncodingPolicy.STRICT_UTF8:
            try:
                content.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise DiscoveryError(
                    "DECODE_FAILED", "The bounded sample is not strict UTF-8."
                ) from error
            return "utf-8"
        ascii_ratio = sum(byte in (9, 10, 13) or 32 <= byte <= 126 for byte in content) / len(
            content
        )
        if ascii_ratio >= 0.85:
            return "ascii"
        try:
            content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return "cp500"
        return "utf-8"

    @staticmethod
    def _check_cancelled(cancellation: CancellationToken | None) -> None:
        if cancellation is not None and cancellation.is_cancelled:
            raise DiscoveryError("CANCELLED", "The operation was cancelled.")

    @staticmethod
    def _emit(
        callback: Callable[[DiscoveryProgress], None] | None,
        event: str,
        file_count: int,
        total_bytes: int,
    ) -> None:
        if callback is not None:
            callback(
                DiscoveryProgress(
                    event=event,
                    file_count=file_count,
                    total_bytes=total_bytes,
                )
            )
