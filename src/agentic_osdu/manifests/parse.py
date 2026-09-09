"""TOOL-014/015 bounded manifest parsing and stable record extraction."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid5

from pydantic import ValidationError

from agentic_osdu.domain.models import (
    DatasetReference,
    ManifestDocumentRef,
    ManifestJsonDocument,
    ManifestRecordRef,
    OSDUKind,
    WorkspaceRelativePath,
)
from agentic_osdu.policy import AuthorizedPath, PolicyViolation
from agentic_osdu.tools.contracts import (
    ExtractManifestRecordsOutput,
    ManifestComponentRelationship,
    ManifestRecordSet,
    ParsedManifest,
    ParseManifestsInput,
    ParseManifestsOutput,
)

PARSER_VERSION = "1.0.0"
CancellationCheck = Callable[[], bool]
_READ_CHUNK_BYTES = 64 * 1024
_GENERATION_MARKERS = frozenset(
    {
        "x-agentic-generation-policy",
        "x-agentic-generation-lineage",
    }
)


class ManifestWorkspace(Protocol):
    workspace_id: UUID

    @property
    def source_root(self) -> str: ...

    def authorize_read(
        self,
        relative_path: str,
        *,
        follow_links: bool = False,
    ) -> AuthorizedPath: ...


class ManifestError(RuntimeError):
    """Stable bounded manifest-tool failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ManifestService:
    """Read manifests exclusively through an approved workspace capability."""

    def __init__(self, workspace: ManifestWorkspace) -> None:
        self._workspace = workspace

    def parse_manifests(
        self,
        request: ParseManifestsInput,
        *,
        cancellation: CancellationCheck | None = None,
    ) -> ParseManifestsOutput:
        if request.workspace_id != self._workspace.workspace_id:
            raise ManifestError("MANIFEST_PATH_DENIED", "The workspace capability does not match.")
        try:
            root_authorized = self._workspace.authorize_read(
                request.manifest_root.root, follow_links=False
            )
        except PolicyViolation as error:
            raise ManifestError(
                "MANIFEST_PATH_DENIED", "The manifest root is not approved."
            ) from error
        root = Path(root_authorized.canonical_path)
        if not root.is_dir():
            raise ManifestError("MANIFEST_PATH_DENIED", "The manifest root is not a directory.")

        paths = self._selected_paths(root, request.paths, cancellation)
        manifests = tuple(
            self._parse_one(
                request.manifest_root,
                path,
                request.max_bytes,
                cancellation,
            )
            for path in paths
        )
        return ParseManifestsOutput(manifests=manifests)

    def _selected_paths(
        self,
        root: Path,
        requested: tuple[WorkspaceRelativePath, ...],
        cancellation: CancellationCheck | None,
    ) -> tuple[Path, ...]:
        if requested:
            paths: Iterator[Path] = (root.joinpath(*path.root.split("/")) for path in requested)
        else:
            paths = self._discover_json_paths(root, cancellation)
        selected: list[Path] = []
        try:
            for path in paths:
                _check_cancelled(cancellation)
                if path.suffix.casefold() != ".json":
                    continue
                relative = path.relative_to(Path(self._workspace.source_root)).as_posix()
                authorized = self._workspace.authorize_read(relative, follow_links=False)
                candidate = Path(authorized.canonical_path)
                if candidate.is_file():
                    selected.append(candidate)
        except (OSError, ValueError, PolicyViolation) as error:
            raise ManifestError(
                "MANIFEST_PATH_DENIED", "A manifest path is not approved."
            ) from error
        return tuple(
            sorted(
                selected,
                key=lambda item: (
                    item.relative_to(root).as_posix().casefold(),
                    item.relative_to(root).as_posix(),
                ),
            )
        )

    @staticmethod
    def _discover_json_paths(
        root: Path,
        cancellation: CancellationCheck | None,
    ) -> Iterator[Path]:
        pending = [root]
        while pending:
            _check_cancelled(cancellation)
            directory = pending.pop()
            entries: list[Path] = []
            for entry in directory.iterdir():
                entries.append(entry)
                _check_cancelled(cancellation)
            entries.sort(
                key=lambda item: (item.name.casefold(), item.name),
                reverse=True,
            )
            child_directories: list[Path] = []
            for entry in entries:
                _check_cancelled(cancellation)
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    child_directories.append(entry)
                elif entry.suffix.casefold() == ".json":
                    yield entry
            pending.extend(child_directories)

    def _parse_one(
        self,
        manifest_root: WorkspaceRelativePath,
        path: Path,
        max_bytes: int,
        cancellation: CancellationCheck | None,
    ) -> ParsedManifest:
        _check_cancelled(cancellation)
        try:
            size = path.stat(follow_symlinks=False).st_size
            if size > max_bytes:
                raise ManifestError(
                    "MANIFEST_TOO_LARGE", "A manifest exceeds the configured parse bound."
                )
            with path.open("rb") as stream:
                chunks: list[bytes] = []
                remaining = max_bytes
                while remaining:
                    chunk = stream.read(min(_READ_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                    _check_cancelled(cancellation)
                raw = b"".join(chunks)
                if remaining == 0 and stream.read(1):
                    raise ManifestError(
                        "MANIFEST_TOO_LARGE",
                        "A manifest exceeds the configured parse bound.",
                    )
        except ManifestError:
            raise
        except OSError as error:
            raise ManifestError("MANIFEST_PATH_DENIED", "A manifest could not be read.") from error
        if len(raw) > max_bytes:
            raise ManifestError(
                "MANIFEST_TOO_LARGE", "A manifest exceeds the configured parse bound."
            )
        _check_cancelled(cancellation)
        try:
            value = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ManifestError("MANIFEST_JSON_INVALID", "A manifest is not valid JSON.") from error
        if not isinstance(value, dict):
            raise ManifestError(
                "MANIFEST_STRUCTURE_UNSUPPORTED", "A manifest must be a JSON object."
            )
        digest = sha256(raw).hexdigest()
        relative_to_source = path.relative_to(Path(self._workspace.source_root))
        relative = WorkspaceRelativePath(relative_to_source.as_posix())
        kind = _kind_or_none(value.get("kind"))
        manifest_id = uuid5(
            self._workspace.workspace_id,
            f"manifest:{relative.root}:{digest}",
        )
        content = ManifestJsonDocument(sha256=digest, content=value)
        return ParsedManifest(
            document=ManifestDocumentRef(
                manifest_id=manifest_id,
                path=relative,
                sha256=digest,
                document_kind=kind,
                generated=(
                    relative.root.casefold().startswith(
                        f"{manifest_root.root.casefold()}/generated/"
                    )
                    or has_generation_lineage(value)
                ),
            ),
            content=content,
            parser_version=PARSER_VERSION,
            parsed_at=datetime.now(UTC),
        )


def extract_manifest_records(
    manifest: ParsedManifest,
    *,
    cancellation: CancellationCheck | None = None,
) -> ExtractManifestRecordsOutput:
    """Extract stable top-level records, reference pointers, and component links."""

    content = manifest.content.content
    data = content.get("Data")
    if not isinstance(data, dict):
        raise ManifestError("MANIFEST_STRUCTURE_UNSUPPORTED", "Manifest Data must be an object.")
    records: list[ManifestRecordRef] = []
    relationships: list[ManifestComponentRelationship] = []
    sections: tuple[tuple[str, object], ...] = (
        ("WorkProduct", data.get("WorkProduct")),
        ("WorkProductComponents", data.get("WorkProductComponents", [])),
        ("Datasets", data.get("Datasets", [])),
    )
    for section, raw_records in sections:
        _check_cancelled(cancellation)
        values = [raw_records] if isinstance(raw_records, dict) else raw_records
        if not isinstance(values, list):
            raise ManifestError(
                "MANIFEST_STRUCTURE_UNSUPPORTED", f"Manifest {section} has an unsupported shape."
            )
        for index, item in enumerate(values):
            _check_cancelled(cancellation)
            if not isinstance(item, dict):
                continue
            pointer = (
                f"/Data/{section}" if isinstance(raw_records, dict) else f"/Data/{section}/{index}"
            )
            kind = _required_kind(item.get("kind"))
            record_id = str(item.get("id") or pointer)
            records.append(
                ManifestRecordRef(
                    manifest_id=manifest.document.manifest_id,
                    record_id=record_id,
                    kind=kind,
                    json_pointer=pointer,
                    surrogate_ids=tuple(sorted(set(_surrogate_strings(item)))),
                )
            )
            if section == "WorkProduct":
                relationships.extend(
                    _relationships(
                        record_id,
                        item,
                        pointer,
                        "component",
                        "Components",
                    )
                )
            elif section == "WorkProductComponents":
                relationships.extend(
                    _relationships(record_id, item, pointer, "dataset", "Datasets")
                )

    record_ids = {record.json_pointer: record.record_id for record in records}
    references = tuple(
        DatasetReference(
            manifest_id=manifest.document.manifest_id,
            record_id=_record_for_pointer(pointer, record_ids),
            value=value,
            normalized_value=normalize_manifest_path(value),
            json_pointer=pointer,
        )
        for pointer, value in _walk_path_strings(content)
    )
    return ExtractManifestRecordsOutput(
        record_set=ManifestRecordSet(
            manifest_id=manifest.document.manifest_id,
            records=tuple(records),
        ),
        dataset_references=references,
        component_relationships=tuple(relationships),
    )


def normalize_manifest_path(value: str) -> str:
    """Normalize a path or URI without accessing it."""

    from urllib.parse import unquote, urlparse

    parsed = urlparse(value)
    path = unquote(parsed.path if parsed.scheme else value)
    normalized = path.replace("\\", "/").strip().casefold()
    marker = "/volve/"
    if marker in normalized:
        normalized = normalized.split(marker, 1)[1]
    return normalized.lstrip("/")


def has_generation_lineage(value: object) -> bool:
    """Return whether immutable TOOL-018 generation metadata is present."""

    return isinstance(value, dict) and bool(_GENERATION_MARKERS.intersection(value))


def _kind_or_none(value: object) -> OSDUKind | None:
    if not isinstance(value, str):
        return None
    try:
        return OSDUKind(value)
    except ValidationError:
        return None


def _required_kind(value: object) -> OSDUKind:
    kind = _kind_or_none(value)
    if kind is None:
        raise ManifestError("KIND_INVALID", "A manifest record kind is missing or invalid.")
    return kind


def _surrogate_strings(value: object) -> Iterator[str]:
    if isinstance(value, str) and value.startswith("surrogate-key:"):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _surrogate_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _surrogate_strings(child)


def _relationships(
    source_id: str,
    record: dict[str, Any],
    pointer: str,
    relationship_type: str,
    field: str,
) -> list[ManifestComponentRelationship]:
    data = record.get("data")
    targets = data.get(field, []) if isinstance(data, dict) else []
    if not isinstance(targets, list):
        return []
    return [
        ManifestComponentRelationship(
            source_record_id=source_id,
            target_record_id=target,
            relationship_type=relationship_type,
            json_pointer=f"{pointer}/data/{field}/{index}",
        )
        for index, target in enumerate(targets)
        if isinstance(target, str) and target
    ]


def _walk_path_strings(value: object, pointer: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        suffix = Path(normalized).suffix
        if "/" in normalized or (suffix and ":" not in normalized):
            yield pointer, value
    elif isinstance(value, dict):
        for key, child in value.items():
            escaped = key.replace("~", "~0").replace("/", "~1")
            yield from _walk_path_strings(child, f"{pointer}/{escaped}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_path_strings(child, f"{pointer}/{index}")


def _record_for_pointer(pointer: str, records: dict[str, str]) -> str | None:
    candidates = [
        (record_pointer, record_id)
        for record_pointer, record_id in records.items()
        if pointer == record_pointer or pointer.startswith(f"{record_pointer}/")
    ]
    return max(candidates, default=(None, None), key=lambda item: len(item[0] or ""))[1]


def _check_cancelled(cancellation: CancellationCheck | None) -> None:
    if cancellation is not None and cancellation():
        raise ManifestError("CANCELLED", "Manifest processing was cancelled.")
