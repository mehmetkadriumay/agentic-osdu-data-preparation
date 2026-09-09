from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agentic_osdu.domain.models import (
    ClassificationDimensions,
    ClassificationRecord,
    DataCategory,
    DataDomain,
    DataSubtype,
    FileAssetRef,
    FormatId,
    LearningExampleRef,
    ManifestDocumentRef,
    ManifestJsonDocument,
    OSDUKind,
    ProcessingLevel,
    ReviewStatus,
    StackType,
    SurveyType,
    TrustLevel,
    WellDataType,
    WorkspaceRelativePath,
)
from agentic_osdu.manifests.learn import LearningError, LearningMaterial, learn_manifest_patterns
from agentic_osdu.tools.contracts import (
    FileRecordContract,
    LearningModelContract,
    LearnManifestPatternsInput,
    ParsedManifest,
)


def _material(
    *,
    source_path: str,
    example_id: UUID | None = None,
    generated: bool = False,
    component_version: str = "1.3.0",
    source_value: str = "Volve",
    owner: str = "owner",
    uri_prefix: str = "s3://bucket/",
    category: DataCategory | None = DataCategory.SEISMIC,
) -> LearningMaterial:
    example = LearningExampleRef.model_construct(
        example_id=example_id or uuid4(),
        source_file_id=uuid4(),
        manifest_id=uuid4(),
        association_id=uuid4(),
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        review_status=ReviewStatus.APPROVED,
        generated_manifest=generated,
    )
    content: dict[str, Any] = {
        "kind": "osdu:wks:Manifest:1.0.0",
        "Data": {
            "WorkProduct": {
                "id": "surrogate-key:wp-1",
                "kind": "osdu:wks:work-product--WorkProduct:1.0.0",
                "acl": {"owners": [owner], "viewers": ["viewer"]},
                "legal": {"legaltags": ["tag"], "otherRelevantDataCountries": ["NO"]},
                "data": {"Source": source_value, "Name": "prototype"},
            },
            "WorkProductComponents": [
                {
                    "id": "surrogate-key:wpc-1",
                    "kind": (
                        f"osdu:wks:work-product-component--SeismicTraceData:{component_version}"
                    ),
                    "acl": {"owners": ["owner"], "viewers": ["viewer"]},
                    "data": {"Source": source_value, "Datasets": ["surrogate-key:file-1"]},
                }
            ],
            "Datasets": [
                {
                    "id": "surrogate-key:file-1",
                    "kind": "osdu:wks:dataset--FileCollection.SEGY:1.1.0",
                    "data": {
                        "DatasetProperties": {
                            "FileSourceInfo": {"FileSource": f"{uri_prefix}{source_path}"}
                        }
                    },
                }
            ],
        },
    }
    digest = (
        __import__("hashlib")
        .sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode())
        .hexdigest()
    )
    example = example.model_copy(update={"manifest_sha256": digest})
    manifest = ParsedManifest(
        document=ManifestDocumentRef(
            manifest_id=example.manifest_id,
            path=WorkspaceRelativePath("Manifests/prototype.json"),
            sha256=digest,
            document_kind=OSDUKind("osdu:wks:Manifest:1.0.0"),
        ),
        content=ManifestJsonDocument(sha256=digest, content=content),
        parser_version="1.0.0",
        parsed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    file = FileRecordContract(
        file=FileAssetRef(
            file_id=example.source_file_id,
            workspace_id=uuid4(),
            relative_path=WorkspaceRelativePath(source_path),
            size_bytes=100,
            modified_at=datetime(2026, 1, 1, tzinfo=UTC),
            sha256=example.source_sha256,
            discovery_version=1,
        ),
        classification=(
            ClassificationRecord(
                classification_id=uuid4(),
                file_id=example.source_file_id,
                format_id=FormatId.SEGY,
                category=category,
                subtype=DataSubtype.SEGY,
                dimensions=ClassificationDimensions(),
                stack=StackType.POST_STACK,
                domain=DataDomain.TIME,
                processing=ProcessingLevel.PROCESSED,
                survey=SurveyType.THREE_D,
                well=WellDataType.NOT_APPLICABLE,
                confidence=1.0,
                detection_id=uuid4(),
                extraction_ids=(),
                evidence_ids=(),
                trust_level=TrustLevel.DERIVED,
            )
            if category is not None
            else None
        ),
    )
    return LearningMaterial(example=example, file_record=file, manifest=manifest)


def test_learning_deduplicates_cumulatively_and_preserves_model_lineage() -> None:
    first = _material(source_path="Data/a.sgy")
    request = LearnManifestPatternsInput(
        examples=(first.example, first.example),
        category=DataCategory.SEISMIC,
        learning_policy_version="1.0.0",
    )
    initial = learn_manifest_patterns(request, (first, first))
    assert initial.model.version == 1
    assert initial.model.example_ids == (first.example.example_id,)
    assert initial.model.example_identities == (first.example,)
    assert initial.delta.added_example_ids == (first.example.example_id,)
    assert initial.delta.ignored_duplicate_ids == (first.example.example_id,)
    assert initial.model.prototype_source_path.root == "Data/a.sgy"
    envelope: Any = initial.model.work_product_envelope
    assert envelope["acl"]["owners"] == ["owner"]
    assert {constant.json_pointer for constant in initial.model.constants} >= {
        "/Data/WorkProduct/data/Source",
        "/Data/WorkProductComponents/0/data/Source",
    }

    second = _material(source_path="Data/b.sgy")
    cumulative_request = LearnManifestPatternsInput(
        examples=(first.example, second.example),
        category=DataCategory.SEISMIC,
        learning_policy_version="1.0.0",
    )
    cumulative = learn_manifest_patterns(
        cumulative_request,
        (first, second),
        existing_model=initial.model,
    )
    assert cumulative.model.version == 2
    assert cumulative.model.example_ids == (
        first.example.example_id,
        second.example.example_id,
    )
    assert cumulative.delta.added_example_ids == (second.example.example_id,)
    assert cumulative.delta.ignored_duplicate_ids == (first.example.example_id,)
    assert cumulative.model.model_sha256 != initial.model.model_sha256

    replay = learn_manifest_patterns(
        LearnManifestPatternsInput(
            examples=(first.example, second.example),
            category=DataCategory.SEISMIC,
            learning_policy_version="1.0.0",
        ),
        (first, second),
        existing_model=cumulative.model,
    )
    assert replay.model == cumulative.model
    assert replay.delta.added_example_ids == ()
    assert replay.delta.ignored_duplicate_ids == (
        first.example.example_id,
        second.example.example_id,
    )


def test_delta_only_learning_retains_historical_normalized_material() -> None:
    historical = _material(
        source_path="Data/old.sgy",
        component_version="2.0.0",
        source_value="Historical",
        owner="historical-owner",
        uri_prefix="old://archive/",
    )
    initial = learn_manifest_patterns(
        LearnManifestPatternsInput(
            examples=(historical.example,),
            category=DataCategory.SEISMIC,
            learning_policy_version="1.0.0",
        ),
        (historical,),
    )
    delta = _material(
        source_path="Data/new.sgy",
        component_version="1.0.0",
        source_value="Delta",
        owner="delta-owner",
        uri_prefix="new://drop/",
    )

    cumulative = learn_manifest_patterns(
        LearnManifestPatternsInput(
            examples=(delta.example,),
            category=DataCategory.SEISMIC,
            learning_policy_version="1.0.0",
        ),
        (delta,),
        existing_model=initial.model,
    )

    assert cumulative.model.example_ids == (
        historical.example.example_id,
        delta.example.example_id,
    )
    assert tuple(item.example_id for item in cumulative.model.material_snapshots) == (
        historical.example.example_id,
        delta.example.example_id,
    )
    assert cumulative.model.prototype == initial.model.prototype
    assert cumulative.model.prototype_source_path == historical.file_record.file.relative_path
    assert cumulative.model.file_source_prefix == "old://archive/"
    cumulative_envelope: Any = cumulative.model.work_product_envelope
    assert cumulative_envelope["acl"]["owners"] == ["historical-owner"]
    assert {(constant.json_pointer, constant.value) for constant in cumulative.model.constants} >= {
        ("/Data/WorkProduct/data/Source", "Historical"),
        ("/Data/WorkProductComponents/0/data/Source", "Historical"),
    }

    replay = learn_manifest_patterns(
        LearnManifestPatternsInput(
            examples=(delta.example,),
            category=DataCategory.SEISMIC,
            learning_policy_version="1.0.0",
        ),
        (delta,),
        existing_model=cumulative.model,
    )
    assert replay.model == cumulative.model


def test_learning_rejects_reused_example_id_with_changed_identity() -> None:
    first = _material(source_path="Data/a.sgy")
    initial = learn_manifest_patterns(
        LearnManifestPatternsInput(
            examples=(first.example,),
            category=DataCategory.SEISMIC,
            learning_policy_version="1.0.0",
        ),
        (first,),
    )
    changed_example = first.example.model_copy(update={"association_id": uuid4()})
    changed = LearningMaterial(
        example=changed_example,
        file_record=first.file_record,
        manifest=first.manifest,
    )

    with pytest.raises(LearningError, match="EXAMPLE_CONFLICT"):
        learn_manifest_patterns(
            LearnManifestPatternsInput(
                examples=(changed_example,),
                category=DataCategory.SEISMIC,
                learning_policy_version="1.0.0",
            ),
            (changed,),
            existing_model=initial.model,
        )


def test_learning_model_contract_rejects_incomplete_persisted_identity() -> None:
    first = _material(source_path="Data/a.sgy")
    learned = learn_manifest_patterns(
        LearnManifestPatternsInput(
            examples=(first.example,),
            category=DataCategory.SEISMIC,
            learning_policy_version="1.0.0",
        ),
        (first,),
    ).model
    payload = learned.model_dump(mode="python")
    payload["example_ids"] = (uuid4(),)

    with pytest.raises(
        ValidationError,
        match="example_ids must exactly match persisted example identities",
    ):
        LearningModelContract.model_validate(payload)


def test_learning_rejects_generated_or_unapproved_material() -> None:
    generated = _material(source_path="Data/generated.sgy", generated=True)
    request = LearnManifestPatternsInput.model_construct(
        examples=(generated.example,),
        category=DataCategory.SEISMIC,
        learning_policy_version="1.0.0",
    )
    with pytest.raises(LearningError, match="GENERATED_EXAMPLE_REJECTED"):
        learn_manifest_patterns(request, (generated,))


def test_learning_rejects_empty_and_unresolved_examples() -> None:
    empty = LearnManifestPatternsInput(
        examples=(),
        category=DataCategory.SEISMIC,
        learning_policy_version="1.0.0",
    )
    with pytest.raises(LearningError, match="NO_ELIGIBLE_EXAMPLES"):
        learn_manifest_patterns(empty, ())

    material = _material(source_path="Data/missing.sgy")
    unresolved = LearnManifestPatternsInput(
        examples=(material.example,),
        category=DataCategory.SEISMIC,
        learning_policy_version="1.0.0",
    )
    with pytest.raises(LearningError, match="EXAMPLE_CONFLICT"):
        learn_manifest_patterns(unresolved, ())


@pytest.mark.parametrize("source_hash", [None, "c" * 64])
def test_learning_requires_exact_current_source_hash(source_hash: str | None) -> None:
    material = _material(source_path="Data/hash.sgy")
    changed_file = material.file_record.model_copy(
        update={"file": material.file_record.file.model_copy(update={"sha256": source_hash})}
    )
    changed = LearningMaterial(
        example=material.example,
        file_record=changed_file,
        manifest=material.manifest,
    )
    request = LearnManifestPatternsInput(
        examples=(material.example,),
        category=DataCategory.SEISMIC,
        learning_policy_version="1.0.0",
    )
    with pytest.raises(LearningError, match="EXAMPLE_CONFLICT"):
        learn_manifest_patterns(request, (changed,))


def test_learning_rejects_existing_model_from_another_category_before_materials() -> None:
    material = _material(source_path="Data/category.sgy")
    model = learn_manifest_patterns(
        LearnManifestPatternsInput(
            examples=(material.example,),
            category=DataCategory.SEISMIC,
            learning_policy_version="1.0.0",
        ),
        (material,),
    ).model

    with pytest.raises(LearningError) as raised:
        learn_manifest_patterns(
            LearnManifestPatternsInput(
                examples=(material.example,),
                category=DataCategory.WELL_LOG,
                learning_policy_version="1.0.0",
            ),
            (),
            existing_model=model,
        )
    assert raised.value.code == "EXAMPLE_CONFLICT"


@pytest.mark.parametrize("category", [None, DataCategory.WELL_LOG])
def test_learning_requires_matching_typed_classification_category(
    category: DataCategory | None,
) -> None:
    material = _material(source_path="Data/category.sgy", category=category)

    with pytest.raises(LearningError) as raised:
        learn_manifest_patterns(
            LearnManifestPatternsInput(
                examples=(material.example,),
                category=DataCategory.SEISMIC,
                learning_policy_version="1.0.0",
            ),
            (material,),
        )

    assert raised.value.code == "EXAMPLE_CONFLICT"


@pytest.mark.parametrize("category", [None, DataCategory.WELL_LOG])
def test_learning_validates_category_evidence_for_every_supplied_material(
    category: DataCategory | None,
) -> None:
    requested = _material(source_path="Data/requested.sgy")
    conflicting_extra = _material(source_path="Data/extra.sgy", category=category)

    with pytest.raises(LearningError) as raised:
        learn_manifest_patterns(
            LearnManifestPatternsInput(
                examples=(requested.example,),
                category=DataCategory.SEISMIC,
                learning_policy_version="1.0.0",
            ),
            (requested, conflicting_extra),
        )

    assert raised.value.code == "EXAMPLE_CONFLICT"


def test_learning_rejects_generation_markers_even_when_flags_and_path_are_bypassed() -> None:
    material = _material(source_path="Data/generated-bypass.sgy")
    content = material.manifest.content.content | {
        "x-agentic-generation-policy": {"version": "1.0.0", "sha256": "c" * 64},
        "x-agentic-generation-lineage": {
            "source_sha256": material.example.source_sha256,
            "model_sha256": "d" * 64,
        },
    }
    digest = (
        __import__("hashlib")
        .sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode())
        .hexdigest()
    )
    example = material.example.model_copy(
        update={"manifest_sha256": digest, "generated_manifest": False}
    )
    marked = LearningMaterial(
        example=example,
        file_record=material.file_record,
        manifest=material.manifest.model_copy(
            update={
                "document": material.manifest.document.model_copy(
                    update={"sha256": digest, "generated": False}
                ),
                "content": ManifestJsonDocument(sha256=digest, content=content),
            }
        ),
    )

    with pytest.raises(LearningError) as raised:
        learn_manifest_patterns(
            LearnManifestPatternsInput(
                examples=(example,),
                category=DataCategory.SEISMIC,
                learning_policy_version="1.0.0",
            ),
            (marked,),
        )
    assert raised.value.code == "GENERATED_EXAMPLE_REJECTED"


@pytest.mark.parametrize("cancel_on_call", [2, 3])
def test_learning_cancels_between_examples_and_before_return(cancel_on_call: int) -> None:
    first = _material(source_path="Data/cancel-a.sgy")
    second = _material(source_path="Data/cancel-b.sgy")
    calls = 0

    def cancellation() -> bool:
        nonlocal calls
        calls += 1
        return calls == cancel_on_call

    with pytest.raises(LearningError) as raised:
        learn_manifest_patterns(
            LearnManifestPatternsInput(
                examples=(first.example, second.example),
                category=DataCategory.SEISMIC,
                learning_policy_version="1.0.0",
            ),
            (first, second),
            cancellation=cancellation,
        )
    assert raised.value.code == "CANCELLED"
