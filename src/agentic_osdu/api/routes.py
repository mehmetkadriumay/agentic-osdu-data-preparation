"""Typed EPIC-009 routes that delegate exclusively to registered tools."""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, Request
from pydantic import BaseModel, ValidationError

from agentic_osdu.tools.contracts import (
    AssociationSnapshotRef,
    BuildInventoryReviewInput,
    BuildManifestReviewInput,
    CancelJobInput,
    ClassifyDataInput,
    ClassifyDataOutput,
    DiscoverFilesInput,
    DiscoveryOutput,
    ExportToolInput,
    ExportToolOutput,
    GenerateAllManifestsInput,
    GenerateManifestInput,
    GenerateManifestOutput,
    InventoryReviewView,
    InventorySnapshotRef,
    JobControlAction,
    JobControlOutput,
    JobDefinition,
    JobStepDefinition,
    LearningModelVersionOutput,
    LearnManifestPatternsInput,
    LearnManifestPatternsOutput,
    ManifestReviewView,
    MatchManifestInput,
    MatchManifestOutput,
    ParseManifestsInput,
    ParseManifestsOutput,
    PersistAssociationsInput,
    PersistInventoryInput,
    PersistLearningModelInput,
    QueryJobEventsInput,
    QueryOrCancelJobInput,
    RecordReviewDecisionInput,
    RecordReviewDecisionOutput,
    RegisterWorkspaceInput,
    ToolRequest,
    ToolResult,
    TrackJobInput,
    TrackJobOutput,
    ValidateSchemasInput,
    ValidationReport,
    WorkspaceDescriptor,
)


class RegisteredToolInvoker(Protocol):
    def invoke(self, tool_id: str, request: ToolRequest[Any]) -> ToolResult[Any]: ...


def _invoke(request: Request, tool_id: str, value: ToolRequest[Any]) -> ToolResult[Any]:
    invoker: RegisteredToolInvoker = request.app.state.tool_invoker
    return invoker.invoke(tool_id, value)


def _parse(model: type[BaseModel], value: dict[str, Any]) -> ToolRequest[Any]:
    try:
        return ToolRequest[model].model_validate_json(  # type: ignore[valid-type]
            __import__("json").dumps(value)
        )
    except ValidationError as error:
        from fastapi.exceptions import RequestValidationError

        raise RequestValidationError(error.errors()) from error


def _request_schema(model: type[BaseModel]) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": ToolRequest[model].model_json_schema()  # type: ignore[valid-type]
                }
            },
        }
    }


def _job_control_request(
    value: ToolRequest[Any], action: JobControlAction
) -> ToolRequest[QueryOrCancelJobInput]:
    control = QueryOrCancelJobInput(
        action=action,
        job_id=value.input.job_id,
        after_event_sequence=getattr(value.input, "after_event_sequence", None),
    )
    return ToolRequest[QueryOrCancelJobInput](
        request_id=value.request_id,
        workspace_id=value.workspace_id,
        actor=value.actor,
        input=control,
        cancellation_token_id=value.cancellation_token_id,
        expected_state_version=value.expected_state_version,
    )


router = APIRouter(prefix="/api/v1")


@router.post(
    "/workspaces",
    operation_id="registerWorkspace",
    response_model=ToolResult[WorkspaceDescriptor],
    openapi_extra=_request_schema(RegisterWorkspaceInput),
)
def register_workspace(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-001", _parse(RegisterWorkspaceInput, value))


@router.post(
    "/inventories/discover",
    operation_id="discoverInventory",
    response_model=ToolResult[DiscoveryOutput],
    openapi_extra=_request_schema(DiscoverFilesInput),
)
def discover_inventory(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-002", _parse(DiscoverFilesInput, value))


@router.post(
    "/inventories/classify",
    operation_id="classifyInventoryItem",
    response_model=ToolResult[ClassifyDataOutput],
    openapi_extra=_request_schema(ClassifyDataInput),
)
def classify_inventory_item(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-005", _parse(ClassifyDataInput, value))


@router.post(
    "/inventories/reset",
    operation_id="resetInventory",
    response_model=ToolResult[InventorySnapshotRef],
    openapi_extra=_request_schema(PersistInventoryInput),
)
def reset_inventory(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-022", _parse(PersistInventoryInput, value))


@router.post(
    "/jobs",
    operation_id="createJob",
    response_model=ToolResult[TrackJobOutput],
    openapi_extra=_request_schema(TrackJobInput),
)
def create_job(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-025", _parse(TrackJobInput, value))


@router.post(
    "/jobs/control",
    operation_id="queryOrCancelJob",
    response_model=ToolResult[JobControlOutput],
    openapi_extra=_request_schema(QueryOrCancelJobInput),
)
def control_job(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-026", _parse(QueryOrCancelJobInput, value))


@router.post(
    "/jobs/events",
    operation_id="queryJobEvents",
    response_model=ToolResult[JobControlOutput],
    openapi_extra=_request_schema(QueryJobEventsInput),
)
def query_job_events(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    parsed = _parse(QueryJobEventsInput, value)
    return _invoke(request, "TOOL-026", _job_control_request(parsed, JobControlAction.QUERY))


@router.post(
    "/jobs/cancel",
    operation_id="cancelJob",
    response_model=ToolResult[JobControlOutput],
    openapi_extra=_request_schema(CancelJobInput),
)
def cancel_job(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    parsed = _parse(CancelJobInput, value)
    return _invoke(request, "TOOL-026", _job_control_request(parsed, JobControlAction.CANCEL))


@router.post(
    "/manifests",
    operation_id="parseManifests",
    response_model=ToolResult[ParseManifestsOutput],
    openapi_extra=_request_schema(ParseManifestsInput),
)
def parse_manifests(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-014", _parse(ParseManifestsInput, value))


@router.post(
    "/associations/match",
    operation_id="matchManifest",
    response_model=ToolResult[MatchManifestOutput],
    openapi_extra=_request_schema(MatchManifestInput),
)
def match_manifest(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-016", _parse(MatchManifestInput, value))


@router.post(
    "/associations",
    operation_id="persistAssociations",
    response_model=ToolResult[AssociationSnapshotRef],
    openapi_extra=_request_schema(PersistAssociationsInput),
)
def persist_associations(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-023", _parse(PersistAssociationsInput, value))


@router.post(
    "/learning/learn",
    operation_id="learnManifestPatterns",
    response_model=ToolResult[LearnManifestPatternsOutput],
    openapi_extra=_request_schema(LearnManifestPatternsInput),
)
def learn_patterns(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-017", _parse(LearnManifestPatternsInput, value))


@router.post(
    "/learning/models",
    operation_id="mutateLearningModel",
    response_model=ToolResult[LearningModelVersionOutput],
    openapi_extra=_request_schema(PersistLearningModelInput),
)
def mutate_learning_model(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-024", _parse(PersistLearningModelInput, value))


@router.post(
    "/generation/one",
    operation_id="generateOneManifest",
    response_model=ToolResult[GenerateManifestOutput],
    openapi_extra=_request_schema(GenerateManifestInput),
)
def generate_one(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    parsed = _parse(GenerateManifestInput, value)
    if not parsed.input.dry_run:
        from agentic_osdu.agents.orchestrator import OrchestrationError

        raise OrchestrationError(
            "HUMAN_APPROVAL_REQUIRED",
            "Write-mode generation must execute through the approved workflow boundary.",
        )
    return _invoke(request, "TOOL-018", parsed)


@router.post(
    "/generation/all",
    operation_id="generateAllManifests",
    response_model=ToolResult[TrackJobOutput],
    openapi_extra=_request_schema(GenerateAllManifestsInput),
)
def generate_all(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    parsed = _parse(GenerateAllManifestsInput, value)
    generation = parsed.input
    if not generation.dry_run:
        from agentic_osdu.agents.orchestrator import OrchestrationError

        raise OrchestrationError(
            "HUMAN_APPROVAL_REQUIRED",
            "Write-mode generation must execute through the approved workflow boundary.",
        )
    job_request = parsed.model_copy(
        update={
            "input": TrackJobInput(
                workflow_id="WF-005",
                generation=generation,
                definition=JobDefinition(
                    job_type="WF-005",
                    steps=(
                        JobStepDefinition(
                            sequence=1,
                            tool_id="TOOL-019",
                            input_ref="generation",
                        ),
                        JobStepDefinition(
                            sequence=2,
                            tool_id="TOOL-020",
                            input_ref="generated-candidates",
                        ),
                        JobStepDefinition(
                            sequence=3,
                            tool_id="TOOL-022",
                            input_ref="validated-candidates",
                        ),
                        JobStepDefinition(
                            sequence=4,
                            tool_id="TOOL-023",
                            input_ref="candidate-associations",
                        ),
                        JobStepDefinition(
                            sequence=5,
                            tool_id="TOOL-027",
                            input_ref="review-projection",
                        ),
                    ),
                    max_concurrency=1,
                    continue_on_error=generation.continue_on_error,
                ),
                deduplication_key=f"WF-005:{parsed.request_id}",
            )
        }
    )
    return _invoke(request, "TOOL-025", job_request)


@router.post(
    "/validation",
    operation_id="validateManifest",
    response_model=ToolResult[ValidationReport],
    openapi_extra=_request_schema(ValidateSchemasInput),
)
def validate_manifest(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-020", _parse(ValidateSchemasInput, value))


@router.post(
    "/review/inventories",
    operation_id="reviewInventory",
    response_model=ToolResult[InventoryReviewView],
    openapi_extra=_request_schema(BuildInventoryReviewInput),
)
def review_inventory(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-027", _parse(BuildInventoryReviewInput, value))


@router.post(
    "/review/manifests",
    operation_id="reviewManifest",
    response_model=ToolResult[ManifestReviewView],
    openapi_extra=_request_schema(BuildManifestReviewInput),
)
def review_manifest(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-028", _parse(BuildManifestReviewInput, value))


@router.post(
    "/review/decisions",
    operation_id="recordReviewDecision",
    response_model=ToolResult[RecordReviewDecisionOutput],
    openapi_extra=_request_schema(RecordReviewDecisionInput),
)
def record_review_decision(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-029", _parse(RecordReviewDecisionInput, value))


@router.post(
    "/exports",
    operation_id="exportReviewResult",
    response_model=ToolResult[ExportToolOutput],
    openapi_extra=_request_schema(ExportToolInput),
)
def export_review(value: dict[str, Any], request: Request) -> ToolResult[Any]:
    return _invoke(request, "TOOL-030", _parse(ExportToolInput, value))


__all__ = ["RegisteredToolInvoker", "router"]
