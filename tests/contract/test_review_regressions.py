from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentic_osdu.domain.models import (
    ApprovedAbsolutePath,
    EvidenceRecord,
    ProvenanceRecord,
    ToolError,
    ToolErrorCategory,
    TrustLevel,
    WorkspaceRelativePath,
)
from agentic_osdu.tools.contracts import (
    TOOL_REGISTRY,
    DiscoverFilesInput,
    LearningModelMutation,
    LearningMutationAction,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    WorkspaceDescriptor,
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)
SHA256 = "0" * 64


def _result(
    status: ToolResultStatus,
    *,
    output: WorkspaceDescriptor | None,
    errors: tuple[ToolError, ...],
) -> ToolResult[WorkspaceDescriptor]:
    request_id = uuid4()
    return ToolResult[WorkspaceDescriptor](
        request_id=request_id,
        tool_id="TOOL-001",
        tool_version="1.0.0",
        status=status,
        output=output,
        errors=errors,
        provenance=(
            ProvenanceRecord(
                provenance_id=uuid4(),
                source_type="workspace",
                source_ref="approved-root",
                tool_id="TOOL-001",
                tool_version="1.0.0",
                recorded_at=NOW,
            ),
        ),
        evidence=(
            EvidenceRecord(
                evidence_id=uuid4(),
                evidence_type="policy",
                rule_id="SEC-001",
                summary="Approved root policy evaluated.",
                trust_level=TrustLevel.VERIFIED,
            ),
        ),
        trust_level=TrustLevel.VERIFIED,
        started_at=NOW,
        finished_at=NOW,
    )


def _workspace() -> WorkspaceDescriptor:
    return WorkspaceDescriptor(
        workspace_id=uuid4(),
        canonical_root=ApprovedAbsolutePath(root=r"C:\approved"),
        read_only=True,
        allowed_output_subpaths=(WorkspaceRelativePath(root="generated"),),
        policy_fingerprint=SHA256,
    )


def _error() -> ToolError:
    return ToolError(
        code="PARTIAL_FAILURE",
        category=ToolErrorCategory.IO,
        message="One bounded operation failed.",
        retryable=True,
    )


@pytest.mark.parametrize(
    ("status", "output", "errors"),
    [
        (ToolResultStatus.SUCCEEDED, _workspace(), (_error(),)),
        (ToolResultStatus.PARTIALLY_SUCCEEDED, None, ()),
        (ToolResultStatus.PARTIALLY_SUCCEEDED, _workspace(), ()),
        (ToolResultStatus.PARTIALLY_SUCCEEDED, None, (_error(),)),
        (ToolResultStatus.CANCELLED, None, ()),
    ],
)
def test_tool_result_rejects_contradictory_states(
    status: ToolResultStatus,
    output: WorkspaceDescriptor | None,
    errors: tuple[ToolError, ...],
) -> None:
    with pytest.raises(ValidationError):
        _result(status, output=output, errors=errors)


def test_discovery_contract_has_bounded_byte_budget() -> None:
    schema = ToolRequest[DiscoverFilesInput].model_json_schema()
    input_properties = schema["$defs"]["DiscoverFilesInput"]["properties"]
    assert input_properties["max_total_bytes"]["exclusiveMinimum"] == 0


@pytest.mark.parametrize(
    "action",
    [
        LearningMutationAction.CREATE,
        LearningMutationAction.ACTIVATE,
        LearningMutationAction.DEACTIVATE,
    ],
)
def test_learning_mutation_rejects_missing_action_fields(
    action: LearningMutationAction,
) -> None:
    with pytest.raises(ValidationError):
        LearningModelMutation.model_validate({"action": action})


def test_review_and_export_registry_metadata_inherits_trust() -> None:
    for tool_id in ("TOOL-027", "TOOL-028", "TOOL-030"):
        assert TOOL_REGISTRY[tool_id].result_trust == "inherited"
