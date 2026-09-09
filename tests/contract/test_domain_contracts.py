from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from inspect import getmembers, isclass
from pathlib import Path
from typing import cast, get_args
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

import agentic_osdu.domain.models as domain_models
from agentic_osdu.domain.models import (
    DOMAIN_MODELS,
    ActorRef,
    ApprovedAbsolutePath,
    ClassificationDimensions,
    ClassificationRecord,
    ContractModel,
    ContractRootModel,
    DataCategory,
    DataDomain,
    DatasetReference,
    DataSubtype,
    EvidenceRecord,
    FormatCandidate,
    FormatDetectionResult,
    FormatId,
    GeneratedCandidateRef,
    JobEventRef,
    JobEventType,
    LearningExampleRef,
    ManifestAssociation,
    ManifestDocumentRef,
    ManifestRecordRef,
    OSDUKind,
    ProcessingLevel,
    ProvenanceRecord,
    ReviewDecision,
    ReviewDecisionValue,
    ReviewStatus,
    ReviewTargetType,
    SideEffectKind,
    SideEffectRecord,
    StackType,
    SurveyType,
    ToolError,
    ToolErrorCategory,
    TrustLevel,
    WellDataType,
    WorkspaceRelativePath,
)


def test_every_domain_model_has_a_stable_strict_json_schema() -> None:
    assert DOMAIN_MODELS
    assert len(DOMAIN_MODELS) == len(set(DOMAIN_MODELS))
    declared_models = {
        model
        for _, model in getmembers(domain_models, isclass)
        if issubclass(model, BaseModel)
        and model.__module__ == domain_models.__name__
        and model not in {ContractModel, ContractRootModel}
    }
    assert set(DOMAIN_MODELS) == declared_models

    for model in DOMAIN_MODELS:
        first = json.dumps(model.model_json_schema(), sort_keys=True)
        second = json.dumps(model.model_json_schema(), sort_keys=True)
        assert first == second
        assert '"title"' in first


def test_domain_json_schemas_match_approved_snapshots() -> None:
    snapshot_path = Path(__file__).parent / "snapshots" / "domain-schema-hashes.json"
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    actual = {
        model.__name__: sha256(
            json.dumps(
                model.model_json_schema(),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        for model in DOMAIN_MODELS
    }
    assert actual == expected


def test_required_format_catalog_is_exact() -> None:
    assert [member.value for member in FormatId] == [f"FMT-{number:03d}" for number in range(1, 14)]


def test_path_contracts_distinguish_relative_and_approved_absolute_paths() -> None:
    relative = WorkspaceRelativePath("Data/well.las")
    absolute = ApprovedAbsolutePath(r"C:\Approved\Data")

    assert relative.root == "Data/well.las"
    assert absolute.root == r"C:\Approved\Data"
    assert relative.model_dump(mode="json") == "Data/well.las"
    assert absolute.model_dump(mode="json") == r"C:\Approved\Data"

    for invalid in ("../escape", r"C:\escape", r"\\server\share", "/absolute"):
        with pytest.raises(ValidationError):
            WorkspaceRelativePath(invalid)
    with pytest.raises(ValidationError):
        ApprovedAbsolutePath("relative/path")


def test_classification_contract_validates_bounds_and_is_immutable() -> None:
    detection = FormatDetectionResult(
        file_id=uuid4(),
        candidates=(
            FormatCandidate(
                format_id=FormatId.SEGY,
                confidence=0.95,
                evidence_ids=(uuid4(),),
            ),
        ),
        detector_version="1.2.3",
    )
    classification = ClassificationRecord(
        classification_id=uuid4(),
        file_id=detection.file_id,
        format_id=FormatId.SEGY,
        category=DataCategory.SEISMIC,
        subtype=DataSubtype.SEGY,
        dimensions=ClassificationDimensions(inline_count=100, crossline_count=50),
        stack=StackType.POST_STACK,
        domain=DataDomain.TIME,
        processing=ProcessingLevel.PROCESSED,
        survey=SurveyType.THREE_D,
        well=WellDataType.NOT_APPLICABLE,
        osdu_kind=OSDUKind("osdu:wks:dataset--Seismic3D:1.0.0"),
        confidence=0.92,
        detection_id=detection.detection_id,
        extraction_ids=(),
        evidence_ids=(uuid4(),),
        trust_level=TrustLevel.DERIVED,
    )

    assert classification.model_dump(mode="json")["format_id"] == "FMT-001"
    with pytest.raises(ValidationError):
        ClassificationRecord(**{**classification.model_dump(), "confidence": 1.01})
    with pytest.raises(ValidationError):
        classification.confidence = 0.5  # type: ignore[misc]


def test_manifest_generation_and_review_contract_invariants() -> None:
    now = datetime.now(UTC)
    manifest = ManifestDocumentRef(
        manifest_id=uuid4(),
        path=WorkspaceRelativePath("Manifests/example.json"),
        sha256="a" * 64,
        document_kind=OSDUKind("osdu:wks:Manifest:1.0.0"),
    )
    record = ManifestRecordRef(
        manifest_id=manifest.manifest_id,
        record_id="record-1",
        kind=OSDUKind("osdu:wks:master-data--Well:1.0.0"),
        json_pointer="/MasterData/0",
        surrogate_ids=("surrogate-1",),
    )
    dataset = DatasetReference(
        manifest_id=manifest.manifest_id,
        record_id=record.record_id,
        value="Data/well.las",
        normalized_value="data/well.las",
        json_pointer="/Data/0/Datasets/0",
        relative_path=WorkspaceRelativePath("Data/well.las"),
    )
    association = ManifestAssociation(
        association_id=uuid4(),
        file_id=uuid4(),
        manifest_id=manifest.manifest_id,
        score=0.8,
        method="exact_dataset_path",
        evidence_ids=(uuid4(),),
    )
    candidate = GeneratedCandidateRef(
        candidate_id=uuid4(),
        source_file_id=association.file_id,
        learning_model_id=uuid4(),
        candidate_sha256="b" * 64,
        proposed_path=WorkspaceRelativePath("generated/example.json"),
    )
    decision = ReviewDecision(
        decision_id=uuid4(),
        actor=ActorRef(actor_id="local-reviewer", display_name="Local Reviewer"),
        target_type=ReviewTargetType.GENERATED_CANDIDATE,
        target_id=candidate.candidate_id,
        target_version=candidate.candidate_sha256,
        decision=ReviewDecisionValue.NEEDS_CHANGES,
        reason="Correct the legal data classification before approval.",
        decided_at=now,
    )

    assert association.review_status is ReviewStatus.PROPOSED
    assert candidate.trust_level is TrustLevel.HEURISTIC
    assert candidate.review_required is True
    assert decision.decision is ReviewDecisionValue.NEEDS_CHANGES
    assert dataset.model_dump(mode="json")["relative_path"] == "Data/well.las"
    with pytest.raises(ValidationError):
        GeneratedCandidateRef(
            candidate_id=uuid4(),
            source_file_id=uuid4(),
            learning_model_id=uuid4(),
            candidate_sha256="c" * 64,
            proposed_path=WorkspaceRelativePath("generated/bad.json"),
            trust_level=TrustLevel.AUTHORITATIVE,
        )


def test_evidence_errors_and_side_effects_are_bounded_and_json_serializable() -> None:
    evidence = EvidenceRecord(
        evidence_id=uuid4(),
        evidence_type="signature",
        rule_id="SEGY-MAGIC-001",
        summary="Binary header signature matched.",
        location="byte-range:3200-3600",
        observed_value="matched",
        trust_level=TrustLevel.VERIFIED,
    )
    error = ToolError(
        code="PATH_OUTSIDE_ROOT",
        category=ToolErrorCategory.ACCESS_DENIED,
        message="The requested path is outside its approved root.",
        retryable=False,
        path=WorkspaceRelativePath("Data/link"),
        details={"path_classification": "link_escape"},
    )
    effect = SideEffectRecord(
        side_effect_id=uuid4(),
        kind=SideEffectKind.NONE,
        target="workspace",
        description="Read-only contract evaluation.",
        occurred_at=datetime.now(UTC),
    )

    assert json.loads(evidence.model_dump_json())["trust_level"] == "verified"
    assert json.loads(error.model_dump_json())["code"] == "PATH_OUTSIDE_ROOT"
    assert json.loads(effect.model_dump_json())["kind"] == "none"
    assert get_args(type(error).model_fields["details"].annotation)

    with pytest.raises(ValidationError):
        ToolError(
            code="not stable",
            category=ToolErrorCategory.INTERNAL,
            message="bad",
            retryable=False,
        )
    with pytest.raises(ValidationError):
        EvidenceRecord(
            evidence_id=uuid4(),
            evidence_type="x",
            rule_id="RULE",
            summary="x" * 1025,
            trust_level=TrustLevel.UNTRUSTED,
        )


def test_scalar_and_structured_contracts_reject_hostile_or_unbounded_values() -> None:
    for invalid in ("", "\x00", "folder//file", "folder/./file"):
        with pytest.raises(ValidationError):
            WorkspaceRelativePath(invalid)
    for invalid in ("", "\x00", "not:a:valid:kind:shape"):
        with pytest.raises(ValidationError):
            OSDUKind(invalid)
    with pytest.raises(ValidationError):
        ApprovedAbsolutePath("\x00")
    with pytest.raises(ValidationError):
        ProvenanceRecord(
            provenance_id=uuid4(),
            source_type="file",
            source_ref="file-id",
            tool_id="TOOL-001",
            tool_version="1.0.0",
            recorded_at=datetime(2026, 1, 1),
        )
    for details in (
        {"password": "not-allowed"},
        {"items": list(range(65))},
        {"x" * 129: "value"},
        {"value": "x" * 2049},
    ):
        with pytest.raises(ValidationError):
            ToolError.model_validate(
                {
                    "code": "DETAILS_INVALID",
                    "category": ToolErrorCategory.VALIDATION,
                    "message": "Invalid structured details.",
                    "retryable": False,
                    "details": details,
                }
            )


def test_nested_structured_contract_values_are_deeply_immutable() -> None:
    error = ToolError(
        code="SAFE_ERROR",
        category=ToolErrorCategory.VALIDATION,
        message="Safe error.",
        retryable=False,
        details={"nested": {"values": ["safe"]}},
    )
    event = JobEventRef(
        job_id=uuid4(),
        sequence=1,
        event_type=JobEventType.CREATED,
        timestamp=datetime.now(UTC),
        counts={"created": 1},
        safe_payload={"stage": {"name": "queued"}},
    )

    with pytest.raises(TypeError):
        error.details["new"] = "mutation"
    nested_details = cast(dict[str, object], error.details["nested"])
    with pytest.raises(TypeError):
        nested_details["new"] = "mutation"
    nested_values = cast(list[object], nested_details["values"])
    with pytest.raises(TypeError):
        nested_values.append("mutation")
    with pytest.raises(TypeError):
        event.counts["created"] = 2
    stage = cast(dict[str, object], event.safe_payload["stage"])
    with pytest.raises(TypeError):
        stage["name"] = "running"


def test_review_decisions_reject_unsupported_target_types() -> None:
    with pytest.raises(ValidationError):
        ReviewDecision(
            decision_id=uuid4(),
            actor=ActorRef(actor_id="reviewer"),
            target_type=cast(ReviewTargetType, "workspace"),
            target_id=uuid4(),
            target_version="1",
            decision=ReviewDecisionValue.APPROVE,
            reason="Only candidates and associations are reviewable.",
            decided_at=datetime.now(UTC),
        )


def test_detection_learning_and_job_payload_invariants_fail_closed() -> None:
    candidate_low = FormatCandidate(
        format_id=FormatId.LAS,
        confidence=0.2,
        evidence_ids=(),
    )
    candidate_high = FormatCandidate(
        format_id=FormatId.SEGY,
        confidence=0.8,
        evidence_ids=(),
    )
    with pytest.raises(ValidationError):
        FormatDetectionResult(
            file_id=uuid4(),
            candidates=(candidate_low, candidate_high),
            detector_version="1.0.0",
        )
    with pytest.raises(ValidationError):
        FormatDetectionResult(
            file_id=uuid4(),
            candidates=(candidate_high, candidate_high),
            detector_version="1.0.0",
        )

    with pytest.raises(ValidationError):
        LearningExampleRef(
            example_id=uuid4(),
            source_file_id=uuid4(),
            manifest_id=uuid4(),
            association_id=uuid4(),
            source_sha256="e" * 64,
            manifest_sha256="f" * 64,
            review_status=ReviewStatus.APPROVED,
            generated_manifest=True,
        )
    with pytest.raises(ValidationError):
        LearningExampleRef(
            example_id=uuid4(),
            source_file_id=uuid4(),
            manifest_id=uuid4(),
            association_id=uuid4(),
            source_sha256="e" * 64,
            manifest_sha256="f" * 64,
            review_status=ReviewStatus.PROPOSED,
        )

    event = JobEventRef(
        job_id=uuid4(),
        sequence=1,
        event_type=JobEventType.CREATED,
        timestamp=datetime.now(UTC),
        safe_payload={"stage": "queued"},
    )
    assert event.safe_payload == {"stage": "queued"}
