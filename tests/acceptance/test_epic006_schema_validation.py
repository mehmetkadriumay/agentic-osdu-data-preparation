from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

from agentic_osdu.domain.models import (
    ManifestDocumentRef,
    ManifestJsonDocument,
    OSDUKind,
    WorkspaceRelativePath,
)
from agentic_osdu.schemas.catalog import SchemaCatalogStore
from agentic_osdu.schemas.validate import SchemaValidationService
from agentic_osdu.tools.contracts import (
    LocalSchemaCatalogImport,
    ParsedManifest,
    SchemaCatalogSource,
    SchemaChecksum,
    ValidateSchemasInput,
)


def _write_schema(root: Path, relative: str, schema: dict[str, object]) -> SchemaChecksum:
    payload = json.dumps({"schema": schema}, sort_keys=True).encode()
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return SchemaChecksum(
        relative_path=WorkspaceRelativePath(relative), sha256=sha256(payload).hexdigest()
    )


def test_tool_020_validates_manifest_and_contained_records_against_exact_kinds(
    tmp_path: Path,
) -> None:
    source = tmp_path / "export"
    checksums = (
        _write_schema(
            source,
            "manifest/Manifest.1.0.0.json",
            {
                "$id": "osdu:wks:Manifest:1.0.0",
                "type": "object",
                "required": ["kind", "Data"],
                "properties": {"kind": {"const": "osdu:wks:Manifest:1.0.0"}},
            },
        ),
        _write_schema(
            source,
            "master-data/Well.1.0.0.json",
            {
                "$id": "{{schema-authority}}:wks:master-data--Well:1.0.0",
                "type": "object",
                "allOf": [{"$ref": "tenant:wks:AbstractLegalParentList:1.0.0"}],
                "required": ["id", "kind", "data"],
                "properties": {
                    "kind": {"const": "tenant:wks:master-data--Well:1.0.0"},
                    "data": {
                        "type": "object",
                        "required": ["Name"],
                        "properties": {"Name": {"type": "string", "minLength": 1}},
                    },
                },
            },
        ),
        _write_schema(
            source,
            "abstract/AbstractLegalParentList.1.0.0.json",
            {
                "$id": "{{NAMESPACE}}:wks:AbstractLegalParentList:1.0.0",
                "type": "object",
            },
        ),
    )
    store = SchemaCatalogStore(tmp_path / "cache")
    descriptor = store.import_local(
        LocalSchemaCatalogImport(
            source=SchemaCatalogSource.LOCAL_EXPORT,
            revision="osdu-r1",
            local_root=str(source),
            expected_checksums=checksums,
        )
    )
    document: dict[str, JsonValue] = {
        "kind": "osdu:wks:Manifest:1.0.0",
        "Data": {
            "WorkProduct": {
                "id": "surrogate-key:wp",
                "kind": "tenant:wks:master-data--Well:1.0.0",
                "data": {"Name": "Volve"},
            }
        },
    }
    encoded = json.dumps(document).encode()
    parsed = ParsedManifest(
        document=ManifestDocumentRef(
            manifest_id=uuid4(),
            path=WorkspaceRelativePath("manifests/valid.json"),
            sha256=sha256(encoded).hexdigest(),
            document_kind=OSDUKind("osdu:wks:Manifest:1.0.0"),
        ),
        content=ManifestJsonDocument(
            sha256=sha256(encoded).hexdigest(),
            content=document,
        ),
        parser_version="1.0.0",
        parsed_at=datetime.now(UTC),
    )

    report = SchemaValidationService(store).validate(
        ValidateSchemasInput(
            manifest=parsed,
            schema_catalog_id=descriptor.schema_catalog_id,
            max_errors=10,
        )
    )

    assert report.status.value == "valid"
    assert report.schema_conformant is True
    assert report.semantic_correctness == "not_assessed"
    assert report.validated_record_count == 1
    assert report.validated_schemas == (
        "osdu:wks:Manifest:1.0.0",
        "tenant:wks:master-data--Well:1.0.0",
    )


def test_tool_020_reports_exact_json_pointer_and_bounded_errors(tmp_path: Path) -> None:
    source = tmp_path / "export"
    checksums = (
        _write_schema(
            source,
            "manifest/Manifest.1.0.0.json",
            {"$id": "osdu:wks:Manifest:1.0.0", "type": "object"},
        ),
        _write_schema(
            source,
            "master-data/Well.1.0.0.json",
            {
                "$id": "tenant:wks:master-data--Well:1.0.0",
                "type": "object",
                "properties": {
                    "data": {
                        "type": "object",
                        "required": ["Name", "Description"],
                    }
                },
            },
        ),
    )
    store = SchemaCatalogStore(tmp_path / "cache")
    descriptor = store.import_local(
        LocalSchemaCatalogImport(
            source=SchemaCatalogSource.LOCAL_EXPORT,
            revision="r1",
            local_root=str(source),
            expected_checksums=checksums,
        )
    )
    document: dict[str, JsonValue] = {
        "kind": "osdu:wks:Manifest:1.0.0",
        "Data": {
            "Datasets": [
                {
                    "id": "surrogate-key:dataset",
                    "kind": "tenant:wks:master-data--Well:1.0.0",
                    "data": {},
                }
            ]
        },
    }
    encoded = json.dumps(document).encode()
    parsed = ParsedManifest(
        document=ManifestDocumentRef(
            manifest_id=uuid4(),
            path=WorkspaceRelativePath("manifests/invalid.json"),
            sha256=sha256(encoded).hexdigest(),
            document_kind=OSDUKind("osdu:wks:Manifest:1.0.0"),
        ),
        content=ManifestJsonDocument(sha256=sha256(encoded).hexdigest(), content=document),
        parser_version="1.0.0",
        parsed_at=datetime.now(UTC),
    )

    report = SchemaValidationService(store).validate(
        ValidateSchemasInput(
            manifest=parsed,
            schema_catalog_id=descriptor.schema_catalog_id,
            max_errors=1,
        )
    )

    assert report.status.value == "invalid"
    assert report.schema_conformant is False
    assert report.errors_truncated is True
    assert len(report.issues) == 1
    assert report.issues[0].json_pointer.startswith("/Data/Datasets/0")
