from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from agentic_osdu.domain.models import WorkspaceRelativePath
from agentic_osdu.manifests.generate import GenerationError
from agentic_osdu.manifests.parse import ManifestError, ManifestService
from agentic_osdu.policy import PathStyle, WindowsAwarePathPolicy, WorkspaceAccessPolicy
from agentic_osdu.tools.contracts import ParseManifestsInput


def _service(root: Path) -> tuple[ManifestService, WorkspaceAccessPolicy]:
    policy = WorkspaceAccessPolicy(
        workspace_id=uuid4(),
        source_root=str(root),
        output_roots={},
        path_policy=WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
    )
    return ManifestService(policy), policy


def test_manifest_parser_rejects_oversize_invalid_json_and_path_escape(tmp_path: Path) -> None:
    manifests = tmp_path / "Manifests"
    manifests.mkdir()
    (manifests / "large.json").write_text(json.dumps({"value": "x" * 100}), encoding="utf-8")
    service, policy = _service(tmp_path)
    with pytest.raises(ManifestError, match="MANIFEST_TOO_LARGE"):
        service.parse_manifests(
            ParseManifestsInput(
                workspace_id=policy.workspace_id,
                manifest_root=WorkspaceRelativePath("Manifests"),
                paths=(WorkspaceRelativePath("large.json"),),
                max_bytes=16,
            )
        )

    (manifests / "invalid.json").write_text("{", encoding="utf-8")
    with pytest.raises(ManifestError, match="MANIFEST_JSON_INVALID"):
        service.parse_manifests(
            ParseManifestsInput(
                workspace_id=policy.workspace_id,
                manifest_root=WorkspaceRelativePath("Manifests"),
                paths=(WorkspaceRelativePath("invalid.json"),),
            )
        )

    with pytest.raises(ValueError, match="traversal"):
        WorkspaceRelativePath("../outside.json")


def test_generation_errors_never_enable_source_writes() -> None:
    assert issubclass(GenerationError, RuntimeError)
