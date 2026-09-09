from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from agentic_osdu.domain.models import (
    ManifestDocumentRef,
    ManifestJsonDocument,
    WorkspaceRelativePath,
)
from agentic_osdu.schemas.catalog import SchemaCatalogError, SchemaCatalogStore
from agentic_osdu.schemas.validate import (
    SchemaValidationService,
    manifest_records,
    surrogate_id_map,
)
from agentic_osdu.tools.contracts import (
    LocalSchemaCatalogImport,
    ParsedManifest,
    SchemaCatalogSource,
    SchemaChecksum,
    ValidateSchemasInput,
)


def _service(tmp_path: Path) -> tuple[SchemaValidationService, UUID]:
    source = tmp_path / "source"
    schemas = {
        "manifest/Manifest.1.0.0.json": {
            "$id": "osdu:wks:Manifest:1.0.0",
            "type": "object",
        },
        "master-data/Well.1.0.0.json": {
            "$id": "tenant:wks:master-data--Well:1.0.0",
            "type": "object",
            "required": ["id"],
        },
    }
    checksums = []
    for relative, schema in schemas.items():
        payload = json.dumps({"schema": schema}).encode()
        path = source / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        checksums.append(
            SchemaChecksum(
                relative_path=WorkspaceRelativePath(relative),
                sha256=sha256(payload).hexdigest(),
            )
        )
    store = SchemaCatalogStore(tmp_path / "cache")
    descriptor = store.import_local(
        LocalSchemaCatalogImport(
            source=SchemaCatalogSource.LOCAL_EXPORT,
            revision="r1",
            local_root=str(source),
            expected_checksums=tuple(checksums),
        )
    )
    return SchemaValidationService(store), descriptor.schema_catalog_id


def _parsed(document: dict[str, JsonValue]) -> ParsedManifest:
    payload = json.dumps(document).encode()
    digest = sha256(payload).hexdigest()
    return ParsedManifest(
        document=ManifestDocumentRef(
            manifest_id=uuid4(),
            path=WorkspaceRelativePath("manifest.json"),
            sha256=digest,
        ),
        content=ManifestJsonDocument(sha256=digest, content=document),
        parser_version="1.0.0",
        parsed_at=datetime.now(UTC),
    )


def test_non_manifest_record_and_invalid_document_kinds(tmp_path: Path) -> None:
    service, catalog_id = _service(tmp_path)
    valid = service.validate(
        ValidateSchemasInput(
            manifest=_parsed(
                {
                    "kind": "tenant:wks:master-data--Well:1.0.0",
                    "id": "tenant:master-data--Well:one",
                }
            ),
            schema_catalog_id=catalog_id,
        )
    )
    assert valid.schema_conformant is True
    assert valid.validated_record_count == 1

    documents: tuple[dict[str, JsonValue], ...] = ({}, {"kind": "bad"})
    for document in documents:
        invalid = service.validate(
            ValidateSchemasInput(
                manifest=_parsed(document),
                schema_catalog_id=catalog_id,
            )
        )
        assert invalid.status.value == "invalid"
        assert invalid.issues[0].code == "DOCUMENT_KIND_INVALID"


def test_manifest_record_without_kind_is_invalid(tmp_path: Path) -> None:
    service, catalog_id = _service(tmp_path)
    report = service.validate(
        ValidateSchemasInput(
            manifest=_parsed(
                {
                    "kind": "osdu:wks:Manifest:1.0.0",
                    "Data": {"WorkProductComponents": [{"id": "surrogate-key:wpc"}]},
                }
            ),
            schema_catalog_id=catalog_id,
        )
    )
    assert report.schema_conformant is False
    assert report.issues[0].json_pointer == "/Data/WorkProductComponents/0/kind"

    malformed = service.validate(
        ValidateSchemasInput(
            manifest=_parsed(
                {
                    "kind": "osdu:wks:Manifest:1.0.0",
                    "Data": {
                        "WorkProductComponents": [
                            {"id": "surrogate-key:wpc", "kind": "tenant:wks:bad:1"}
                        ]
                    },
                }
            ),
            schema_catalog_id=catalog_id,
        )
    )
    assert malformed.status.value == "invalid"
    assert malformed.issues[0].code == "DOCUMENT_KIND_INVALID"


def test_record_walking_and_surrogate_mapping_cover_all_sections() -> None:
    record: dict[str, JsonValue] = {
        "id": "ordinary-id",
        "kind": "tenant:wks:master-data--Well:1.0.0",
    }
    records = manifest_records(
        {
            "ReferenceData": [record, "ignored"],
            "MasterData": [record],
            "Data": {
                "Datasets": [record],
                "WorkProductComponents": [record],
                "WorkProduct": record,
            },
        }
    )
    assert [pointer for pointer, _ in records] == [
        "/ReferenceData/0",
        "/MasterData/0",
        "/Data/Datasets/0",
        "/Data/WorkProductComponents/0",
        "/Data/WorkProduct",
    ]
    assert surrogate_id_map(records) == {}


def test_non_object_data_has_no_contained_records() -> None:
    assert manifest_records({"Data": []}) == []


def test_invalid_schema_definition_is_unavailable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    payload = json.dumps(
        {
            "schema": {
                "$id": "tenant:wks:master-data--Well:1.0.0",
                "type": "not-a-json-schema-type",
            }
        }
    ).encode()
    relative = "master-data/Well.1.0.0.json"
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
    report = SchemaValidationService(store).validate(
        ValidateSchemasInput(
            manifest=_parsed(
                {
                    "kind": "tenant:wks:master-data--Well:1.0.0",
                    "id": "tenant:master-data--Well:one",
                }
            ),
            schema_catalog_id=descriptor.schema_catalog_id,
        )
    )
    assert report.status.value == "unavailable"
    assert report.schema_conformant is None


def test_validation_honors_cancellation_between_records(tmp_path: Path) -> None:
    service, catalog_id = _service(tmp_path)
    calls = 0

    def cancellation() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    with pytest.raises(SchemaCatalogError, match="CANCELLED"):
        service.validate(
            ValidateSchemasInput(
                manifest=_parsed(
                    {
                        "kind": "osdu:wks:Manifest:1.0.0",
                        "Data": {
                            "Datasets": [
                                {
                                    "id": "tenant:master-data--Well:one",
                                    "kind": "tenant:wks:master-data--Well:1.0.0",
                                }
                            ]
                        },
                    }
                ),
                schema_catalog_id=catalog_id,
            ),
            cancellation=cancellation,
        )
