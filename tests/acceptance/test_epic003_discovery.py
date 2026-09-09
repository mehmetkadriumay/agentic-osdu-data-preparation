from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from agentic_osdu.domain.models import WorkspaceRelativePath
from agentic_osdu.tools.contracts import (
    DiscoverFilesInput,
    EncodingPolicy,
    ReadFileSampleInput,
    RegisterWorkspaceInput,
    SampleMode,
    WorkspaceDescriptor,
)
from agentic_osdu.tools.discovery import (
    CancellationToken,
    DiscoveryError,
    DiscoveryProgress,
    DiscoveryService,
    InMemoryWorkspacePolicyStore,
)


def register(service: DiscoveryService, root: Path) -> WorkspaceDescriptor:
    return service.register_workspace(
        RegisterWorkspaceInput(
            root_path=str(root),
            allowed_output_subpaths=(WorkspaceRelativePath("generated"),),
        )
    )


def test_workspace_registration_is_canonical_deterministic_and_persisted(tmp_path: Path) -> None:
    store = InMemoryWorkspacePolicyStore()
    service = DiscoveryService(store=store)

    first = register(service, tmp_path)
    second = register(service, tmp_path / ".")

    assert first == second
    assert first.canonical_root.root == str(tmp_path.resolve())
    assert len(first.policy_fingerprint) == 64
    assert store.get(first.workspace_id) == first


def test_discovery_is_ordered_excluded_bounded_and_reports_progress(tmp_path: Path) -> None:
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "second.las").write_text("~Version\n", encoding="ascii")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "first.sgy").write_bytes(b"C 1 TEST")
    (tmp_path / "a" / "ignored.tmp").write_bytes(b"x")
    service = DiscoveryService(store=InMemoryWorkspacePolicyStore(), progress_batch_size=1)
    workspace = register(service, tmp_path)
    progress: list[DiscoveryProgress] = []

    output = service.discover_files(
        DiscoverFilesInput(
            workspace_id=workspace.workspace_id,
            exclude_globs=("**/*.tmp",),
            max_files=2,
            max_total_bytes=100,
        ),
        on_progress=progress.append,
    )

    assert [item.relative_path.root for item in output.files] == [
        "a/first.sgy",
        "B/second.las",
    ]
    assert output.batch.file_count == 2
    assert output.batch.total_bytes == sum(item.size_bytes for item in output.files)
    assert [event.event for event in progress] == [
        "discovery.started",
        "discovery.batch",
        "discovery.batch",
        "discovery.completed",
    ]

    with pytest.raises(DiscoveryError, match="FILE_LIMIT_EXCEEDED"):
        service.discover_files(DiscoverFilesInput(workspace_id=workspace.workspace_id, max_files=1))


def test_discovery_does_not_follow_links_and_honors_cancellation(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "inside.txt").write_text("inside", encoding="ascii")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable for this Windows test account")

    service = DiscoveryService(store=InMemoryWorkspacePolicyStore())
    workspace = register(service, tmp_path)
    output = service.discover_files(
        DiscoverFilesInput(workspace_id=workspace.workspace_id, follow_symlinks=False)
    )
    assert [item.relative_path.root for item in output.files] == ["real/inside.txt"]

    token = CancellationToken()
    token.cancel()
    with pytest.raises(DiscoveryError, match="CANCELLED"):
        service.discover_files(
            DiscoverFilesInput(workspace_id=workspace.workspace_id),
            cancellation=token,
        )


def test_bounded_reads_never_exceed_limit_and_detect_file_changes(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_bytes(b"0123456789")
    service = DiscoveryService(store=InMemoryWorkspacePolicyStore())
    workspace = register(service, tmp_path)
    asset = service.discover_files(DiscoverFilesInput(workspace_id=workspace.workspace_id)).files[0]

    sample = service.read_file_sample(
        ReadFileSampleInput(
            file_id=asset.file_id,
            mode=SampleMode.RANGE,
            offset=3,
            max_bytes=4,
            encoding_policy=EncodingPolicy.BINARY,
        )
    )
    assert sample.decoded_bytes() == b"3456"
    assert sample.sample.length == 4
    assert sample.truncated is True

    text_sample = service.read_file_sample(
        ReadFileSampleInput(
            file_id=asset.file_id,
            mode=SampleMode.TEXT_SAMPLE,
            max_bytes=5,
            encoding_policy=EncodingPolicy.DETECT_BOUNDED,
        )
    )
    assert text_sample.decoded_bytes() == b"01234"
    assert text_sample.text_encoding == "ascii"

    source.write_bytes(b"changed")
    with pytest.raises(DiscoveryError, match="FILE_CHANGED"):
        service.read_file_sample(
            ReadFileSampleInput(
                file_id=asset.file_id,
                mode=SampleMode.PREFIX,
                max_bytes=4,
            )
        )


def test_unknown_workspace_and_file_fail_closed(tmp_path: Path) -> None:
    service = DiscoveryService(store=InMemoryWorkspacePolicyStore())
    with pytest.raises(DiscoveryError, match="ROOT_POLICY_DENIED"):
        service.discover_files(DiscoverFilesInput(workspace_id=uuid4()))
    with pytest.raises(DiscoveryError, match="FILE_NOT_FOUND"):
        service.read_file_sample(
            ReadFileSampleInput(
                file_id=uuid4(),
                mode=SampleMode.PREFIX,
                max_bytes=1,
            )
        )
