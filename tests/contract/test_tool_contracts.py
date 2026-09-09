from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from agentic_osdu.domain.models import (
    ActorRef,
    DataCategory,
    EvidenceRecord,
    GeneratedCandidateRef,
    GeneratedManifestCandidate,
    LearningExampleRef,
    ManifestJsonDocument,
    ProvenanceRecord,
    ReviewStatus,
    TrustLevel,
    ValidationStatus,
    WorkspaceRelativePath,
)
from agentic_osdu.tools.contracts import (
    TOOL_REGISTRY,
    ApprovedRemoteSchemaCatalogRefresh,
    BuildManifestReviewInput,
    ClassifyDataInput,
    CsvMetadata,
    DlisMetadata,
    ExtractManifestRecordsOutput,
    GenerateManifestOutput,
    InventoryMutation,
    InventoryReviewView,
    LearningModelContract,
    LearnManifestPatternsInput,
    LocalSchemaCatalogImport,
    ManifestIndexContract,
    P190Metadata,
    ParsedManifest,
    RefreshSchemaCatalogInput,
    RegisterWorkspaceInput,
    SchemaCatalogSource,
    SchemaChecksum,
    SegyMetadata,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    ValidateSchemasInput,
    ValidationPreflight,
    ValidationReport,
    WorkspaceDescriptor,
    validate_tool_registry,
)


def test_tool_registry_is_complete_ordered_and_concrete() -> None:
    validate_tool_registry(TOOL_REGISTRY)
    assert list(TOOL_REGISTRY) == [f"TOOL-{number:03d}" for number in range(1, 31)]

    input_models: set[type[BaseModel]] = set()
    output_models: set[type[BaseModel]] = set()
    for tool_id, definition in TOOL_REGISTRY.items():
        assert definition.tool_id == tool_id
        assert definition.name
        assert definition.purpose.endswith(".")
        assert definition.version == "1.0.0"
        assert issubclass(definition.input_model, BaseModel)
        assert issubclass(definition.output_model, BaseModel)
        assert definition.input_model.model_fields
        assert definition.output_model.model_fields
        assert definition.input_model.model_json_schema()
        assert definition.output_model.model_json_schema()
        assert ToolRequest[definition.input_model].model_json_schema()  # type: ignore[name-defined]
        assert ToolResult[definition.output_model].model_json_schema()  # type: ignore[name-defined]
        input_models.add(definition.input_model)
        output_models.add(definition.output_model)

    assert len(input_models) == 30
    assert len(output_models) == 30


def test_tool_json_schemas_match_approved_snapshots() -> None:
    snapshot_path = Path(__file__).parent / "snapshots" / "tool-schema-hashes.json"
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    actual: dict[str, str] = {}
    for tool_id, definition in TOOL_REGISTRY.items():
        schemas = {
            "input": definition.input_model.model_json_schema(),
            "output": definition.output_model.model_json_schema(),
            "request": ToolRequest[definition.input_model].model_json_schema(),  # type: ignore[name-defined]
            "result": ToolResult[definition.output_model].model_json_schema(),  # type: ignore[name-defined]
        }
        for schema_name, schema in schemas.items():
            actual[f"{tool_id}:{schema_name}"] = sha256(
                json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
    assert actual == expected


def test_generic_request_has_exact_common_fields_and_is_strict() -> None:
    request_type = ToolRequest[RegisterWorkspaceInput]
    assert set(request_type.model_fields) == {
        "request_id",
        "workspace_id",
        "actor",
        "input",
        "cancellation_token_id",
        "expected_state_version",
    }
    request = request_type(
        request_id=uuid4(),
        workspace_id=uuid4(),
        actor=ActorRef(actor_id="operator"),
        input=RegisterWorkspaceInput(
            root_path=r"C:\Approved",
            read_only=True,
            allowed_output_subpaths=(WorkspaceRelativePath("generated"),),
        ),
    )
    assert request.input.read_only is True
    with pytest.raises(ValidationError):
        request.expected_state_version = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        request_type(
            request_id=str(uuid4()),  # type: ignore[arg-type]
            workspace_id=uuid4(),
            actor=ActorRef(actor_id="operator"),
            input=request.input,
        )


def test_generic_result_has_exact_common_fields_and_status_invariants() -> None:
    result_type = ToolResult[WorkspaceDescriptor]
    assert set(result_type.model_fields) == {
        "request_id",
        "tool_id",
        "tool_version",
        "status",
        "output",
        "errors",
        "provenance",
        "evidence",
        "trust_level",
        "side_effects",
        "started_at",
        "finished_at",
    }
    started = datetime.now(UTC)
    evidence = EvidenceRecord(
        evidence_id=uuid4(),
        evidence_type="canonical_path",
        rule_id="WORKSPACE-ROOT-001",
        summary="The approved root was canonicalized.",
        trust_level=TrustLevel.VERIFIED,
    )
    provenance = ProvenanceRecord(
        provenance_id=uuid4(),
        source_type="workspace_policy",
        source_ref="workspace-root",
        tool_id="TOOL-001",
        tool_version="1.0.0",
        recorded_at=started,
    )
    result = result_type(
        request_id=uuid4(),
        tool_id="TOOL-001",
        tool_version="1.0.0",
        status=ToolResultStatus.SUCCEEDED,
        output=WorkspaceDescriptor(
            workspace_id=uuid4(),
            canonical_root=r"C:\Approved",
            read_only=True,
            allowed_output_subpaths=(WorkspaceRelativePath("generated"),),
            policy_fingerprint="d" * 64,
        ),
        evidence=(evidence,),
        provenance=(provenance,),
        trust_level=TrustLevel.VERIFIED,
        started_at=started,
        finished_at=started + timedelta(milliseconds=1),
    )
    assert result.status is ToolResultStatus.SUCCEEDED
    assert result.errors == ()

    with pytest.raises(ValidationError):
        result_type(
            request_id=uuid4(),
            tool_id="TOOL-001",
            tool_version="1.0.0",
            status=ToolResultStatus.SUCCEEDED,
            output=result.output,
            trust_level=TrustLevel.VERIFIED,
            started_at=started,
            finished_at=started,
        )
    with pytest.raises(ValidationError):
        result_type(
            request_id=uuid4(),
            tool_id="TOOL-001",
            tool_version="1.0.0",
            status=ToolResultStatus.SUCCEEDED,
            output=None,
            trust_level=TrustLevel.UNTRUSTED,
            started_at=started,
            finished_at=started,
        )
    with pytest.raises(ValidationError):
        result_type(
            request_id=uuid4(),
            tool_id="TOOL-001",
            tool_version="1.0.0",
            status=ToolResultStatus.FAILED,
            output=result.output,
            errors=(),
            trust_level=TrustLevel.UNTRUSTED,
            started_at=started,
            finished_at=started,
        )
    with pytest.raises(ValidationError):
        result_type(
            request_id=uuid4(),
            tool_id="TOOL-001",
            tool_version="1.0.0",
            status=ToolResultStatus.CANCELLED,
            output=None,
            trust_level=TrustLevel.UNTRUSTED,
            started_at=started,
            finished_at=started - timedelta(seconds=1),
        )


def test_learning_generation_review_and_inventory_contracts_are_end_to_end() -> None:
    example = LearningExampleRef(
        example_id=uuid4(),
        source_file_id=uuid4(),
        manifest_id=uuid4(),
        association_id=uuid4(),
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        review_status=ReviewStatus.APPROVED,
    )
    learning = LearnManifestPatternsInput(
        examples=(example,),
        category=DataCategory.SEISMIC,
        learning_policy_version="1.0.0",
    )
    assert learning.examples == (example,)

    candidate_ref = GeneratedCandidateRef(
        candidate_id=uuid4(),
        source_file_id=example.source_file_id,
        source_sha256=example.source_sha256,
        learning_model_id=uuid4(),
        model_sha256="d" * 64,
        candidate_sha256="c" * 64,
        proposed_path=WorkspaceRelativePath("generated/candidate.json"),
    )
    document = ManifestJsonDocument(
        sha256=candidate_ref.candidate_sha256,
        content={"kind": "osdu:wks:Manifest:1.0.0", "Data": []},
    )
    candidate = GeneratedManifestCandidate(reference=candidate_ref, document=document)
    generated = GenerateManifestOutput(
        candidate=candidate,
        validation_preflight=ValidationPreflight(
            status=ValidationStatus.NOT_RUN,
            error_count=0,
        ),
    )
    assert generated.candidate.document.model_dump(mode="json")["content"]["Data"] == []
    assert BuildManifestReviewInput(manifest=candidate).manifest == candidate
    assert (
        ValidateSchemasInput(
            manifest=candidate,
            schema_catalog_id=uuid4(),
        ).manifest
        == candidate
    )

    assert {
        "files",
        "detections",
        "extractions",
        "classifications",
        "remove_file_ids",
    }.issubset(InventoryMutation.model_fields)
    assert "request_id" not in InventoryMutation.model_fields


def test_tool_outputs_cover_every_explicit_catalog_field_group() -> None:
    assert "extracted_metadata" in ClassifyDataInput.model_fields
    assert {"dimensions", "binary_header", "survey_metadata", "trace_samples"}.issubset(
        SegyMetadata.model_fields
    )
    assert {"channels", "origins"}.issubset(DlisMetadata.model_fields)
    assert {"sampled_rows"}.issubset(CsvMetadata.model_fields)
    assert {"headers", "line_summaries"}.issubset(P190Metadata.model_fields)
    assert "component_relationships" in ExtractManifestRecordsOutput.model_fields
    assert "content" in ParsedManifest.model_fields
    assert {"records", "dataset_references", "component_relationships"}.issubset(
        ManifestIndexContract.model_fields
    )
    assert {"prototype", "constants"}.issubset(LearningModelContract.model_fields)
    assert {"schema_revision", "catalog_sha256"}.issubset(ValidationReport.model_fields)
    assert {
        "category_summaries",
        "format_summaries",
        "review_status_summaries",
    }.issubset(InventoryReviewView.model_fields)


def test_schema_catalog_source_contract_is_discriminated_and_complete() -> None:
    checksum = SchemaChecksum(
        relative_path=WorkspaceRelativePath("schemas/example.json"),
        sha256="d" * 64,
    )
    local = RefreshSchemaCatalogInput.model_validate(
        {
            "source": SchemaCatalogSource.LOCAL_EXPORT,
            "revision": "v1",
            "local_root": r"C:\Approved\Schemas",
            "expected_checksums": (checksum,),
        }
    )
    assert isinstance(local.root, LocalSchemaCatalogImport)
    remote = RefreshSchemaCatalogInput.model_validate(
        {
            "source": SchemaCatalogSource.APPROVED_REMOTE,
            "revision": "v1",
            "remote_uri": "https://schemas.example.test/catalog",
            "expected_checksums": (checksum,),
            "network_approval_id": uuid4(),
        }
    )
    assert isinstance(remote.root, ApprovedRemoteSchemaCatalogRefresh)

    for incomplete in (
        {
            "source": "local_export",
            "revision": "v1",
            "expected_checksums": (checksum,),
        },
        {
            "source": "approved_remote",
            "revision": "v1",
            "remote_uri": "http://schemas.example.test/catalog",
            "expected_checksums": (checksum,),
            "network_approval_id": uuid4(),
        },
        {
            "source": "approved_remote",
            "revision": "v1",
            "remote_uri": "https://schemas.example.test/catalog",
            "expected_checksums": [],
            "network_approval_id": uuid4(),
        },
    ):
        with pytest.raises(ValidationError):
            RefreshSchemaCatalogInput.model_validate(incomplete)

    assert TOOL_REGISTRY["TOOL-021"].network_access.value == "conditional_approval"
