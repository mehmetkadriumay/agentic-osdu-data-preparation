from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agentic_osdu.domain.models import (
    DataCategory,
    FileAssetRef,
    ManifestDocumentRef,
    ManifestJsonDocument,
    WorkspaceRelativePath,
)
from agentic_osdu.manifests.generate import GenerationError, GenerationItem, GenerationService
from agentic_osdu.manifests.match import MATCHING_POLICY_V1, MatchingError, match_manifest
from agentic_osdu.manifests.parse import ManifestError, ManifestService, extract_manifest_records
from agentic_osdu.policy import PathStyle, WindowsAwarePathPolicy, WorkspaceAccessPolicy
from agentic_osdu.tools.contracts import (
    FileRecordContract,
    GenerateAllManifestsInput,
    GenerateManifestInput,
    GenerationFilters,
    LearningModelContract,
    ManifestIndexContract,
    MatchingPolicyVersion,
    MatchManifestInput,
    ParsedManifest,
    ParseManifestsInput,
)


def _file(path: str = "Data/a.sgy") -> FileRecordContract:
    return FileRecordContract(
        file=FileAssetRef(
            file_id=uuid4(),
            workspace_id=uuid4(),
            relative_path=WorkspaceRelativePath(path),
            size_bytes=1,
            modified_at=datetime(2026, 1, 1, tzinfo=UTC),
            sha256="a" * 64,
            discovery_version=1,
        )
    )


def _model(content: dict[str, Any] | None = None) -> LearningModelContract:
    value = content or {"kind": "osdu:wks:Manifest:1.0.0", "Data": {}}
    digest = (
        __import__("hashlib")
        .sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        .hexdigest()
    )
    return LearningModelContract(
        learning_model_id=uuid4(),
        category=DataCategory.SEISMIC,
        version=1,
        model_sha256="d" * 64,
        example_ids=(),
        example_identities=(),
        prototype=ManifestJsonDocument(sha256=digest, content=value),
        constants=(),
        prototype_source_path=WorkspaceRelativePath("Data/source.sgy"),
        file_source_prefix="",
        work_product_envelope={},
        component_envelope={},
        dataset_envelope={},
    )


def _generation_service(
    tmp_path: Path,
    items: tuple[GenerationItem, ...],
) -> GenerationService:
    tmp_path.mkdir(exist_ok=True)
    output = tmp_path / "generated"
    output.mkdir()
    policy = WorkspaceAccessPolicy(
        workspace_id=uuid4(),
        source_root=str(tmp_path),
        output_roots={"generated": str(output)},
        path_policy=WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
        allowed_source_output_subpaths={"generated": "generated"},
    )
    return GenerationService(policy, items)


def test_parse_and_extract_failures_are_structured_and_cancellable(tmp_path: Path) -> None:
    manifests = tmp_path / "Manifests"
    manifests.mkdir()
    (manifests / "array.json").write_text("[]", encoding="utf-8")
    policy = WorkspaceAccessPolicy(
        workspace_id=uuid4(),
        source_root=str(tmp_path),
        output_roots={},
        path_policy=WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
    )
    service = ManifestService(policy)
    with pytest.raises(ManifestError, match="MANIFEST_PATH_DENIED"):
        service.parse_manifests(
            ParseManifestsInput(
                workspace_id=uuid4(),
                manifest_root=WorkspaceRelativePath("Manifests"),
            )
        )
    with pytest.raises(ManifestError, match="MANIFEST_PATH_DENIED"):
        service.parse_manifests(
            ParseManifestsInput(
                workspace_id=policy.workspace_id,
                manifest_root=WorkspaceRelativePath("missing"),
            )
        )
    with pytest.raises(ManifestError, match="MANIFEST_STRUCTURE_UNSUPPORTED"):
        service.parse_manifests(
            ParseManifestsInput(
                workspace_id=policy.workspace_id,
                manifest_root=WorkspaceRelativePath("Manifests"),
            )
        )
    with pytest.raises(ManifestError, match="CANCELLED"):
        service.parse_manifests(
            ParseManifestsInput(
                workspace_id=policy.workspace_id,
                manifest_root=WorkspaceRelativePath("Manifests"),
            ),
            cancellation=lambda: True,
        )

    value: dict[str, Any] = {
        "kind": "osdu:wks:Manifest:1.0.0",
        "Data": {"WorkProduct": {"id": "x"}},
    }
    digest = __import__("hashlib").sha256(json.dumps(value).encode()).hexdigest()
    parsed = ParsedManifest(
        document=ManifestDocumentRef(
            manifest_id=uuid4(),
            path=WorkspaceRelativePath("Manifests/bad.json"),
            sha256=digest,
        ),
        content=ManifestJsonDocument(sha256=digest, content=value),
        parser_version="1.0.0",
        parsed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ManifestError, match="KIND_INVALID"):
        extract_manifest_records(parsed)


def test_manifest_discovery_cancels_before_enumerating_the_full_tree(tmp_path: Path) -> None:
    manifests = tmp_path / "Manifests"
    for index in range(20):
        directory = manifests / f"{index:02d}"
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text('{"Data": {}}', encoding="utf-8")
    policy = WorkspaceAccessPolicy(
        workspace_id=uuid4(),
        source_root=str(tmp_path),
        output_roots={},
        path_policy=WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
    )

    class CountingWorkspace:
        workspace_id = policy.workspace_id
        source_root = policy.source_root
        authorized_paths = 0

        def authorize_read(self, relative_path: str, *, follow_links: bool = False) -> Any:
            self.authorized_paths += 1
            return policy.authorize_read(relative_path, follow_links=follow_links)

    workspace = CountingWorkspace()
    with pytest.raises(ManifestError, match="CANCELLED"):
        ManifestService(workspace).parse_manifests(
            ParseManifestsInput(
                workspace_id=workspace.workspace_id,
                manifest_root=WorkspaceRelativePath("Manifests"),
            ),
            cancellation=lambda: workspace.authorized_paths >= 5,
        )

    assert workspace.authorized_paths < 21


def test_manifest_discovery_checks_cancellation_during_directory_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = tmp_path / "Manifests"
    manifests.mkdir()
    entries = tuple(manifests / f"{index:02d}.json" for index in range(3))
    original_iterdir = Path.iterdir
    enumerated = 0

    def cancellation_sensitive_iterdir(path: Path) -> Any:
        if path != manifests:
            return original_iterdir(path)

        def iterator() -> Any:
            nonlocal enumerated
            for entry in entries:
                enumerated += 1
                if enumerated > 1:
                    raise AssertionError("directory enumeration continued after cancellation")
                yield entry

        return iterator()

    monkeypatch.setattr(Path, "iterdir", cancellation_sensitive_iterdir)

    with pytest.raises(ManifestError, match="CANCELLED"):
        tuple(
            ManifestService._discover_json_paths(
                manifests,
                cancellation=lambda: enumerated >= 1,
            )
        )

    assert enumerated == 1


def test_manifest_read_cancels_between_bounded_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifests = tmp_path / "Manifests"
    manifests.mkdir()
    manifest = manifests / "large.json"
    manifest.write_text(json.dumps({"Data": {}, "padding": "x" * 150_000}), encoding="utf-8")
    policy = WorkspaceAccessPolicy(
        workspace_id=uuid4(),
        source_root=str(tmp_path),
        output_roots={},
        path_policy=WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
    )
    original_open = Path.open
    read_calls = 0

    class TrackingStream:
        def __init__(self, stream: Any) -> None:
            self._stream = stream

        def __enter__(self) -> TrackingStream:
            self._stream.__enter__()
            return self

        def __exit__(self, *args: Any) -> Any:
            return self._stream.__exit__(*args)

        def read(self, size: int = -1) -> bytes:
            nonlocal read_calls
            read_calls += 1
            result = self._stream.read(size)
            if not isinstance(result, bytes):
                raise TypeError("Binary manifest reads must return bytes.")
            return result

    def tracking_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        stream = original_open(path, *args, **kwargs)
        return TrackingStream(stream) if path == manifest else stream

    monkeypatch.setattr(Path, "open", tracking_open)
    with pytest.raises(ManifestError, match="CANCELLED"):
        ManifestService(policy).parse_manifests(
            ParseManifestsInput(
                workspace_id=policy.workspace_id,
                manifest_root=WorkspaceRelativePath("Manifests"),
                paths=(WorkspaceRelativePath("large.json"),),
                max_bytes=200_000,
            ),
            cancellation=lambda: read_calls >= 2,
        )

    assert read_calls == 2


def test_parser_marks_generation_lineage_outside_generated_directory(tmp_path: Path) -> None:
    category_root = tmp_path / "Manifests" / "Seismic"
    category_root.mkdir(parents=True)
    generated = category_root / "candidate.json"
    generated.write_text(
        json.dumps(
            {
                "kind": "osdu:wks:Manifest:1.0.0",
                "Data": {},
                "x-agentic-generation-policy": {
                    "version": "1.0.0",
                    "sha256": "c" * 64,
                },
                "x-agentic-generation-lineage": {
                    "source_sha256": "a" * 64,
                    "model_sha256": "d" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    policy = WorkspaceAccessPolicy(
        workspace_id=uuid4(),
        source_root=str(tmp_path),
        output_roots={},
        path_policy=WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
    )

    parsed = ManifestService(policy).parse_manifests(
        ParseManifestsInput(
            workspace_id=policy.workspace_id,
            manifest_root=WorkspaceRelativePath("Manifests/Seismic"),
        )
    )

    assert parsed.manifests[0].document.generated is True


def test_matching_rejects_unknown_policy_and_returns_no_match() -> None:
    file_record = _file()
    index = ManifestIndexContract(
        manifest_index_id=uuid4(),
        manifest_ids=(uuid4(),),
        records=(),
        dataset_references=(),
        component_relationships=(),
        index_sha256="a" * 64,
        built_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(MatchingError, match="MATCH_POLICY_UNAVAILABLE"):
        match_manifest(
            MatchManifestInput(
                file_record=file_record,
                manifest_index=index,
                matching_policy=MatchingPolicyVersion(
                    version="2.0.0",
                    policy_sha256="b" * 64,
                ),
            )
        )
    assert (
        match_manifest(
            MatchManifestInput(
                file_record=file_record,
                manifest_index=index,
                matching_policy=MATCHING_POLICY_V1,
            )
        ).matches
        == ()
    )


def test_matching_cancels_between_manifest_iterations() -> None:
    file_record = _file()
    manifest_ids = (uuid4(), uuid4())
    index = ManifestIndexContract(
        manifest_index_id=uuid4(),
        manifest_ids=manifest_ids,
        records=(),
        dataset_references=(),
        component_relationships=(),
        index_sha256="a" * 64,
        built_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    calls = 0

    def cancellation() -> bool:
        nonlocal calls
        calls += 1
        return calls == 2

    with pytest.raises(MatchingError) as raised:
        match_manifest(
            MatchManifestInput(
                file_record=file_record,
                manifest_index=index,
                matching_policy=MATCHING_POLICY_V1,
            ),
            cancellation=cancellation,
        )
    assert raised.value.code == "CANCELLED"


def test_generation_failure_batch_existing_and_stop_modes(tmp_path: Path) -> None:
    model = _model()
    file_record = _file()
    item = GenerationItem(
        file_record=file_record,
        category=DataCategory.SEISMIC,
        format_id=None,
        model=model,
    )
    service = _generation_service(tmp_path, (item,))
    with pytest.raises(GenerationError, match="NO_COMPATIBLE_MODEL"):
        service.generate_one(
            GenerateManifestInput(
                file_id=uuid4(),
                learning_model_id=model.learning_model_id,
                generation_policy_version="1.0.0",
            )
        )
    ineligible = GenerationItem(
        file_record=_file("Data/ineligible.sgy"),
        category=DataCategory.SEISMIC,
        format_id=None,
        model=model,
        eligible=False,
    )
    ineligible_service = _generation_service(tmp_path / "ineligible", (ineligible,))
    with pytest.raises(GenerationError, match="NO_COMPATIBLE_MODEL"):
        ineligible_service.generate_one(
            GenerateManifestInput(
                file_id=ineligible.file_record.file.file_id,
                learning_model_id=model.learning_model_id,
                generation_policy_version="1.0.0",
            )
        )
    request = GenerateAllManifestsInput(
        inventory_id=service.inventory_id,
        generation_policy_version="1.0.0",
        filters=GenerationFilters(),
        continue_on_error=False,
        dry_run=False,
    )
    assert service.generate_all(request).generated == 1
    existing = service.generate_all(request)
    assert existing.existing == 1
    with pytest.raises(GenerationError, match="BATCH_POLICY_INVALID"):
        service.generate_all(request.model_copy(update={"inventory_id": uuid4()}))

    bad_model = _model({"kind": "osdu:wks:Manifest:1.0.0"})
    bad_item = GenerationItem(
        file_record=_file("Data/bad.sgy"),
        category=DataCategory.SEISMIC,
        format_id=None,
        model=bad_model,
    )
    bad_service = _generation_service(tmp_path / "second", (bad_item,))
    result = bad_service.generate_all(
        GenerateAllManifestsInput(
            inventory_id=bad_service.inventory_id,
            generation_policy_version="1.0.0",
            continue_on_error=False,
        )
    )
    assert result.failed == 1
    with pytest.raises(GenerationError, match="CANCELLED"):
        bad_service.generate_one(
            GenerateManifestInput(
                file_id=bad_item.file_record.file.file_id,
                learning_model_id=bad_model.learning_model_id,
                generation_policy_version="1.0.0",
            ),
            cancellation=lambda: True,
        )


@pytest.mark.parametrize(
    "stage",
    ["exists", "mkdir", "open", "write", "flush", "fsync", "link"],
)
def test_all_atomic_output_io_failures_are_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    service = _generation_service(tmp_path, ())
    relative = WorkspaceRelativePath("io/failure.json")
    original_exists = Path.exists
    original_mkdir = Path.mkdir
    original_open = Path.open
    original_fsync = os.fsync
    original_link = os.link
    original_unlink = Path.unlink

    class FailingStream:
        def __enter__(self) -> FailingStream:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def write(self, content: bytes) -> int:
            if stage == "write":
                raise OSError("write failed")
            return len(content)

        def flush(self) -> None:
            if stage == "flush":
                raise OSError("flush failed")

        def fileno(self) -> int:
            return 1

    def failing_exists(path: Path) -> bool:
        if stage == "exists" and path.name == "failure.json":
            raise OSError("exists failed")
        return original_exists(path)

    def failing_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if stage == "mkdir" and path.name == "io":
            raise OSError("mkdir failed")
        original_mkdir(path, *args, **kwargs)

    def failing_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.suffix == ".tmp":
            if stage == "open":
                raise OSError("open failed")
            if stage in {"write", "flush"}:
                return FailingStream()
        return original_open(path, *args, **kwargs)

    def failing_fsync(fd: int) -> None:
        if stage == "fsync":
            raise OSError("fsync failed")
        original_fsync(fd)

    def failing_link(source: Any, target: Any) -> None:
        if stage == "link":
            raise OSError("link failed")
        original_link(source, target)

    def failing_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if stage == "cleanup" and path.suffix == ".tmp":
            raise OSError("cleanup failed")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", failing_exists)
    monkeypatch.setattr(Path, "mkdir", failing_mkdir)
    monkeypatch.setattr(Path, "open", failing_open)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    monkeypatch.setattr(os, "link", failing_link)
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(GenerationError) as raised:
        service._write_atomic(relative, b"content")
    assert raised.value.code == "GENERATION_CONFLICT"


def test_cleanup_failure_does_not_mask_output_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _generation_service(tmp_path, ())
    original_unlink = Path.unlink

    def collision(source: Any, target: Any) -> None:
        raise FileExistsError("collision")

    def cleanup_failure(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.suffix == ".tmp":
            raise OSError("cleanup failed")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "link", collision)
    monkeypatch.setattr(Path, "unlink", cleanup_failure)

    with pytest.raises(GenerationError) as raised:
        service._write_atomic(WorkspaceRelativePath("io/collision.json"), b"content")
    assert raised.value.code == "OUTPUT_EXISTS"


def test_cleanup_failure_after_publication_preserves_success_and_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model()
    file_record = _file("Data/published.sgy")
    item = GenerationItem(file_record, DataCategory.SEISMIC, None, model)
    service = _generation_service(tmp_path, (item,))
    original_unlink = Path.unlink

    def cleanup_failure(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.suffix == ".tmp":
            raise OSError("cleanup failed after publication")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", cleanup_failure)
    output = service.generate_one(
        GenerateManifestInput(
            file_id=file_record.file.file_id,
            learning_model_id=model.learning_model_id,
            generation_policy_version="1.0.0",
            dry_run=False,
        )
    )

    target = tmp_path / "generated" / output.candidate.reference.proposed_path.root
    assert output.candidate.reference.generation_status.value == "generated"
    assert (
        target.read_bytes()
        == (
            json.dumps(
                output.candidate.document.content,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode()
    )


def test_batch_counts_structured_output_io_failures_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model()
    first = GenerationItem(_file("Data/a.sgy"), DataCategory.SEISMIC, None, model)
    second = GenerationItem(_file("Data/b.sgy"), DataCategory.SEISMIC, None, model)
    service = _generation_service(tmp_path, (first, second))

    def fail_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        raise OSError("untranslated output failure")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    result = service.generate_all(
        GenerateAllManifestsInput(
            inventory_id=service.inventory_id,
            generation_policy_version="1.0.0",
            continue_on_error=True,
            dry_run=False,
        )
    )
    assert (result.failed, result.generated, result.existing, result.skipped) == (2, 0, 0, 0)
