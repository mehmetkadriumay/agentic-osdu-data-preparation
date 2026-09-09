from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agentic_osdu.domain.models import (
    ClassificationDimensions,
    ClassificationRecord,
    DataCategory,
    DataDomain,
    DataSubtype,
    FileAssetRef,
    FormatId,
    OSDUKind,
    ProcessingLevel,
    StackType,
    SurveyType,
    TrustLevel,
    WellDataType,
    WorkspaceRelativePath,
)
from agentic_osdu.manifests.match import MATCHING_POLICY_V1, match_manifest
from agentic_osdu.manifests.parse import ManifestService, extract_manifest_records
from agentic_osdu.policy import PathStyle, WindowsAwarePathPolicy, WorkspaceAccessPolicy
from agentic_osdu.tools.contracts import (
    FileRecordContract,
    ManifestIndexContract,
    MatchManifestInput,
    ParseManifestsInput,
)


def _policy(root: Path) -> WorkspaceAccessPolicy:
    return WorkspaceAccessPolicy(
        workspace_id=uuid4(),
        source_root=str(root),
        output_roots={},
        path_policy=WindowsAwarePathPolicy(style=PathStyle.WINDOWS),
    )


def _file(path: str) -> FileRecordContract:
    file_id = uuid4()
    detection_id = uuid4()
    return FileRecordContract(
        file=FileAssetRef(
            file_id=file_id,
            workspace_id=uuid4(),
            relative_path=WorkspaceRelativePath(path),
            size_bytes=10,
            modified_at=datetime(2026, 1, 1, tzinfo=UTC),
            discovery_version=1,
        ),
        classification=ClassificationRecord(
            classification_id=uuid4(),
            file_id=file_id,
            format_id=FormatId.SEGY,
            category=DataCategory.SEISMIC,
            subtype=DataSubtype.SEGY,
            dimensions=ClassificationDimensions(),
            stack=StackType.POST_STACK,
            domain=DataDomain.TIME,
            processing=ProcessingLevel.PROCESSED,
            survey=SurveyType.THREE_D,
            well=WellDataType.NOT_APPLICABLE,
            osdu_kind=OSDUKind("osdu:wks:work-product-component--SeismicTraceData:1.0.0"),
            confidence=1.0,
            detection_id=detection_id,
            extraction_ids=(),
            evidence_ids=(),
            trust_level=TrustLevel.DERIVED,
        ),
    )


def _write_manifest(path: Path, data_path: str) -> None:
    document = {
        "kind": "osdu:wks:Manifest:1.0.0",
        "Data": {
            "WorkProduct": {
                "id": "surrogate-key:wp-1",
                "kind": "osdu:wks:work-product--WorkProduct:1.0.0",
                "data": {"Components": ["surrogate-key:wpc-1"]},
            },
            "WorkProductComponents": [
                {
                    "id": "surrogate-key:wpc-1",
                    "kind": "osdu:wks:work-product-component--SeismicTraceData:1.3.0",
                    "data": {"Datasets": ["surrogate-key:file-1"]},
                }
            ],
            "Datasets": [
                {
                    "id": "surrogate-key:file-1",
                    "kind": "osdu:wks:dataset--FileCollection.SEGY:1.0.0",
                    "data": {
                        "DatasetProperties": {
                            "FileSourceInfo": {
                                "FileSource": data_path,
                                "Name": Path(data_path).name,
                            }
                        }
                    },
                }
            ],
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_manifest_parsing_extraction_and_matching_cover_all_five_precedence_levels(
    tmp_path: Path,
) -> None:
    root = tmp_path
    paths = [
        "Data/exact-dataset.sgy",
        "Data/exact-anywhere.sgy",
        "Data/dataset-name.sgy",
        "Data/anywhere-name.sgy",
        "Data/Normalized-Identifier-12345678.sgy",
    ]
    _write_manifest(root / "Manifests" / "one.json", paths[0])
    _write_manifest(root / "Manifests" / "two.json", "other/two.sgy")
    two = json.loads((root / "Manifests" / "two.json").read_text(encoding="utf-8"))
    two["outside"] = paths[1]
    (root / "Manifests" / "two.json").write_text(json.dumps(two), encoding="utf-8")
    _write_manifest(root / "Manifests" / "three.json", Path(paths[2]).name)
    _write_manifest(root / "Manifests" / "four.json", "other/four.sgy")
    four = json.loads((root / "Manifests" / "four.json").read_text(encoding="utf-8"))
    four["outside"] = Path(paths[3]).name
    (root / "Manifests" / "four.json").write_text(json.dumps(four), encoding="utf-8")
    _write_manifest(
        root / "Manifests" / "Data" / "data_normalized_identifier_12345678_manifest.json",
        "other/five.sgy",
    )

    policy = _policy(root)
    parsed = ManifestService(policy).parse_manifests(
        ParseManifestsInput(
            workspace_id=policy.workspace_id,
            manifest_root=WorkspaceRelativePath("Manifests"),
            max_bytes=32_000,
        )
    )
    extracted = tuple(extract_manifest_records(item) for item in parsed.manifests)
    assert [item.document.path.root for item in parsed.manifests] == [
        "Manifests/Data/data_normalized_identifier_12345678_manifest.json",
        "Manifests/four.json",
        "Manifests/one.json",
        "Manifests/three.json",
        "Manifests/two.json",
    ]
    assert all(item.record_set.records for item in extracted)
    first_records = extracted[0].record_set.records
    all_records = tuple(record for item in extracted for record in item.record_set.records)
    assert all(record.json_pointer.startswith("/Data/") for record in first_records)
    assert all(record.kind.root.count(":") == 3 for record in all_records)

    records = all_records
    references = tuple(reference for item in extracted for reference in item.dataset_references)
    assert all(not reference.value.startswith("osdu:") for reference in references)
    relationships = tuple(
        relationship for item in extracted for relationship in item.component_relationships
    )
    index = ManifestIndexContract(
        manifest_index_id=uuid4(),
        manifest_ids=tuple(item.document.manifest_id for item in parsed.manifests),
        manifest_documents=tuple(item.document for item in parsed.manifests),
        records=records,
        dataset_references=references,
        component_relationships=relationships,
        index_sha256="1" * 64,
        built_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    methods = []
    for path in paths:
        matches = match_manifest(
            MatchManifestInput(
                file_record=_file(path),
                manifest_index=index,
                matching_policy=MATCHING_POLICY_V1,
            )
        ).matches
        assert len(matches) == 1, path
        methods.append(matches[0].method)
        assert matches[0].evidence_ids
    assert methods == [
        "exact_dataset_path",
        "exact_path",
        "exact_dataset_filename",
        "exact_filename",
        "normalized_identifier",
    ]
    assert [
        match_manifest(
            MatchManifestInput(
                file_record=_file(path),
                manifest_index=index,
                matching_policy=MATCHING_POLICY_V1,
            )
        )
        .matches[0]
        .score
        for path in paths
    ] == [1.0, 0.95, 0.85, 0.75, 0.55]
