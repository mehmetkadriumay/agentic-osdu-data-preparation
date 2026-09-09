from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import JsonValue

from agentic_osdu.domain.models import (
    GeneratedCandidateRef,
    GeneratedManifestCandidate,
    ManifestJsonDocument,
    WorkspaceRelativePath,
)
from agentic_osdu.policy import NetworkApproval, NetworkPolicy, NetworkPurpose
from agentic_osdu.schemas.catalog import SchemaCatalogError, SchemaCatalogStore
from agentic_osdu.schemas.validate import SchemaValidationService
from agentic_osdu.tools.contracts import (
    ApprovedRemoteSchemaCatalogRefresh,
    LocalSchemaCatalogImport,
    SchemaCatalogSource,
    SchemaChecksum,
    ValidateSchemasInput,
)


def test_remote_refresh_never_calls_network_without_matching_explicit_approval(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    payload = b'{"schema":{"type":"object"}}'

    def download(url: str) -> bytes:
        calls.append(url)
        return payload

    request = ApprovedRemoteSchemaCatalogRefresh(
        source=SchemaCatalogSource.APPROVED_REMOTE,
        revision="r1",
        remote_uri="https://schemas.example.test/catalog",
        expected_checksums=(
            SchemaChecksum(
                relative_path=WorkspaceRelativePath("manifest/Manifest.1.0.0.json"),
                sha256=sha256(payload).hexdigest(),
            ),
        ),
        network_approval_id=uuid4(),
    )
    store = SchemaCatalogStore(
        tmp_path / "cache",
        network_policy=NetworkPolicy(enabled=False),
        downloader=download,
    )

    with pytest.raises(SchemaCatalogError, match="NETWORK_NOT_APPROVED"):
        store.refresh_remote(request)
    assert calls == []


def test_approved_remote_refresh_checks_revision_path_and_checksum(tmp_path: Path) -> None:
    payload = b'{"schema":{"type":"object"}}'
    now = datetime.now(UTC)
    approval_id = uuid4()
    request = ApprovedRemoteSchemaCatalogRefresh(
        source=SchemaCatalogSource.APPROVED_REMOTE,
        revision="commit-123",
        remote_uri="https://schemas.example.test/catalog",
        expected_checksums=(
            SchemaChecksum(
                relative_path=WorkspaceRelativePath("manifest/Manifest.1.0.0.json"),
                sha256=sha256(payload).hexdigest(),
            ),
        ),
        network_approval_id=approval_id,
    )
    seen: list[str] = []

    def download(url: str) -> bytes:
        seen.append(url)
        return payload

    store = SchemaCatalogStore(
        tmp_path / "cache",
        network_policy=NetworkPolicy(enabled=True),
        downloader=download,
    )
    descriptor = store.refresh_remote(
        request,
        approval=NetworkApproval(
            approval_id=approval_id,
            purpose=NetworkPurpose.SCHEMA_CATALOG_REFRESH,
            approved_hosts=("schemas.example.test",),
            approved_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(minutes=1),
            actor_id="operator",
        ),
        now=now,
    )
    assert descriptor.revision == "commit-123"
    assert seen == ["https://schemas.example.test/catalog/commit-123/manifest/Manifest.1.0.0.json"]


def test_remote_refresh_rejects_mismatched_approval_and_payload(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    request_id = uuid4()
    request = ApprovedRemoteSchemaCatalogRefresh(
        source=SchemaCatalogSource.APPROVED_REMOTE,
        revision="r1",
        remote_uri="https://schemas.example.test/catalog",
        expected_checksums=(
            SchemaChecksum(
                relative_path=WorkspaceRelativePath("manifest/Manifest.1.0.0.json"),
                sha256="0" * 64,
            ),
        ),
        network_approval_id=request_id,
    )
    store = SchemaCatalogStore(
        tmp_path / "cache",
        network_policy=NetworkPolicy(enabled=True),
        downloader=lambda _: b'{"schema":{"type":"object"}}',
    )
    wrong_approval = NetworkApproval(
        approval_id=uuid4(),
        purpose=NetworkPurpose.SCHEMA_CATALOG_REFRESH,
        approved_hosts=("schemas.example.test",),
        approved_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=1),
        actor_id="operator",
    )
    with pytest.raises(SchemaCatalogError, match="NETWORK_NOT_APPROVED"):
        store.refresh_remote(request, approval=wrong_approval, now=now)

    approved = wrong_approval.model_copy(update={"approval_id": request_id})
    with pytest.raises(SchemaCatalogError, match="CHECKSUM_MISMATCH"):
        store.refresh_remote(request, approval=approved, now=now)

    invalid_uri = request.model_copy(update={"remote_uri": "http://schemas.example.test/catalog"})
    with pytest.raises(SchemaCatalogError, match="NETWORK_NOT_APPROVED"):
        store.refresh_remote(invalid_uri, approval=approved, now=now)

    unavailable = SchemaCatalogStore(
        tmp_path / "unavailable",
        network_policy=NetworkPolicy(enabled=True),
        downloader=lambda _: (_ for _ in ()).throw(OSError("offline")),
    )
    with pytest.raises(SchemaCatalogError, match="CATALOG_INCOMPLETE"):
        unavailable.refresh_remote(request, approval=approved, now=now)

    oversized = SchemaCatalogStore(
        tmp_path / "oversized",
        network_policy=NetworkPolicy(enabled=True),
        downloader=lambda _: b"x" * (16 * 1024 * 1024 + 1),
    )
    with pytest.raises(SchemaCatalogError, match="CATALOG_INCOMPLETE"):
        oversized.refresh_remote(request, approval=approved, now=now)


def test_missing_reference_never_reports_manifest_valid(tmp_path: Path) -> None:
    source = tmp_path / "source"
    relative = "manifest/Manifest.1.0.0.json"
    payload = json.dumps(
        {
            "schema": {
                "$id": "osdu:wks:Manifest:1.0.0",
                "type": "object",
                "properties": {
                    "Data": {"$ref": "osdu:wks:AbstractMissing:1.0.0"},
                },
            }
        }
    ).encode()
    path = source / Path(relative)
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    store = SchemaCatalogStore(tmp_path / "cache")
    descriptor = store.import_local(
        LocalSchemaCatalogImport(
            source=SchemaCatalogSource.LOCAL_EXPORT,
            revision="r1",
            local_root=str(source),
            expected_checksums=(
                SchemaChecksum(
                    relative_path=WorkspaceRelativePath(relative),
                    sha256=sha256(payload).hexdigest(),
                ),
            ),
        )
    )
    document: dict[str, JsonValue] = {
        "kind": "osdu:wks:Manifest:1.0.0",
        "Data": {},
    }
    encoded = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    candidate = GeneratedManifestCandidate(
        reference=GeneratedCandidateRef(
            candidate_id=uuid4(),
            source_file_id=uuid4(),
            source_sha256="1" * 64,
            learning_model_id=uuid4(),
            model_sha256="2" * 64,
            candidate_sha256=sha256(encoded).hexdigest(),
            proposed_path=WorkspaceRelativePath("generated/test.json"),
        ),
        document=ManifestJsonDocument(
            sha256=sha256(encoded).hexdigest(),
            content=document,
        ),
    )

    report = SchemaValidationService(store).validate(
        ValidateSchemasInput(
            manifest=candidate,
            schema_catalog_id=descriptor.schema_catalog_id,
        )
    )

    assert report.status.value == "unavailable"
    assert report.schema_conformant is None
    assert report.issues[0].code == "SCHEMA_REFERENCE_FAILED"


def test_unavailable_catalog_never_reports_conformance(tmp_path: Path) -> None:
    document: dict[str, JsonValue] = {
        "kind": "osdu:wks:Manifest:1.0.0",
        "Data": {},
    }
    encoded = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    candidate = GeneratedManifestCandidate(
        reference=GeneratedCandidateRef(
            candidate_id=uuid4(),
            source_file_id=uuid4(),
            source_sha256="1" * 64,
            learning_model_id=uuid4(),
            model_sha256="2" * 64,
            candidate_sha256=sha256(encoded).hexdigest(),
            proposed_path=WorkspaceRelativePath("generated/test.json"),
        ),
        document=ManifestJsonDocument(
            sha256=sha256(encoded).hexdigest(),
            content=document,
        ),
    )
    report = SchemaValidationService(SchemaCatalogStore(tmp_path / "empty")).validate(
        ValidateSchemasInput(manifest=candidate, schema_catalog_id=uuid4())
    )
    assert report.status.value == "unavailable"
    assert report.schema_conformant is None
    assert report.issues[0].code == "SCHEMA_UNAVAILABLE"
