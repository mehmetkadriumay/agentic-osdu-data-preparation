from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agentic_osdu.agents.orchestrator import (
    OrchestrationError,
    Orchestrator,
    ToolInvoker,
    ToolRegistryAdapter,
    WorkflowExecutor,
)
from agentic_osdu.agents.roles import AGENT_ROLES, AgentId, AgentRole
from agentic_osdu.agents.workflows import (
    ExecutionAuthority,
    WorkflowId,
    WorkflowPlan,
    WorkflowStep,
    build_workflow_plan,
)
from agentic_osdu.domain.models import (
    ActorRef,
    EvidenceRecord,
    ProvenanceRecord,
    SideEffectKind,
    SideEffectRecord,
    ToolError,
    ToolErrorCategory,
    TrustLevel,
    WorkspaceRelativePath,
)
from agentic_osdu.tools.contracts import (
    RegisterWorkspaceInput,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    WorkspaceDescriptor,
)


def _request() -> ToolRequest[RegisterWorkspaceInput]:
    workspace_id = uuid4()
    return ToolRequest[RegisterWorkspaceInput](
        request_id=uuid4(),
        workspace_id=workspace_id,
        actor=ActorRef(actor_id=AgentId.WORKSPACE_INTAKE.value),
        input=RegisterWorkspaceInput(
            root_path=r"C:\Approved",
            allowed_output_subpaths=(WorkspaceRelativePath("generated"),),
        ),
    )


def _result(
    request: ToolRequest[RegisterWorkspaceInput],
    *,
    status: ToolResultStatus = ToolResultStatus.SUCCEEDED,
    retryable: bool = False,
) -> ToolResult[WorkspaceDescriptor]:
    now = datetime.now(UTC)
    return ToolResult[WorkspaceDescriptor](
        request_id=request.request_id,
        tool_id="TOOL-001",
        tool_version="1.0.0",
        status=status,
        output=(
            WorkspaceDescriptor(
                workspace_id=request.workspace_id,
                canonical_root=r"C:\Approved",
                read_only=True,
                allowed_output_subpaths=(WorkspaceRelativePath("generated"),),
                policy_fingerprint="a" * 64,
            )
            if status is ToolResultStatus.SUCCEEDED
            else None
        ),
        errors=(
            ToolError(
                code="ROOT_BUSY",
                category=ToolErrorCategory.IO,
                message="The approved root is temporarily unavailable.",
                retryable=retryable,
            ),
        )
        if status is ToolResultStatus.FAILED
        else (),
        provenance=(
            ProvenanceRecord(
                provenance_id=uuid4(),
                source_type="workspace_policy",
                source_ref="approved-root",
                tool_id="TOOL-001",
                tool_version="1.0.0",
                recorded_at=now,
            ),
        ),
        evidence=(
            EvidenceRecord(
                evidence_id=uuid4(),
                evidence_type="canonical_path",
                rule_id="WORKSPACE-ROOT-001",
                summary="The approved root was canonicalized.",
                trust_level=TrustLevel.VERIFIED,
            ),
        ),
        trust_level=TrustLevel.VERIFIED,
        started_at=now,
        finished_at=now,
    )


def test_registry_invokes_only_registered_handlers_with_typed_envelopes() -> None:
    request = _request()
    registry = ToolRegistryAdapter({"TOOL-001": _result})

    result = registry.invoke("TOOL-001", request)

    assert isinstance(result.output, WorkspaceDescriptor)
    assert result.request_id == request.request_id
    assert isinstance(registry, ToolInvoker)
    assert isinstance(Orchestrator(registry), WorkflowExecutor)
    with pytest.raises(OrchestrationError, match="UNREGISTERED_TOOL"):
        registry.invoke("TOOL-002", request)
    registry = ToolRegistryAdapter({"TOOL-002": _result})
    with pytest.raises(OrchestrationError, match="TOOL_INPUT_INVALID"):
        registry.invoke("TOOL-002", request)


def test_orchestrator_retries_only_retryable_failures_with_same_request() -> None:
    request = _request()
    seen_request_ids: list[object] = []

    def flaky(value: ToolRequest[RegisterWorkspaceInput]) -> ToolResult[WorkspaceDescriptor]:
        seen_request_ids.append(value.request_id)
        if len(seen_request_ids) == 1:
            return _result(value, status=ToolResultStatus.FAILED, retryable=True)
        return _result(value)

    registry = ToolRegistryAdapter({"TOOL-001": flaky})
    plan = build_workflow_plan(WorkflowId.WF_001, extraction_tool_ids=())[:1]
    outcome = Orchestrator(registry, max_attempts=2).execute(
        plan,
        {plan.steps[0].step_id: request},
    )

    assert outcome.succeeded is True
    assert outcome.steps[0].attempts == 2
    assert seen_request_ids == [request.request_id, request.request_id]
    assert outcome.narrative[0].trust_level is TrustLevel.VERIFIED
    assert outcome.narrative[0].evidence_ids == tuple(
        item.evidence_id for item in outcome.steps[0].result.evidence
    )


def test_orchestrator_does_not_retry_non_retryable_failures() -> None:
    request = _request()
    calls = 0

    def failing(value: ToolRequest[RegisterWorkspaceInput]) -> ToolResult[WorkspaceDescriptor]:
        nonlocal calls
        calls += 1
        return _result(value, status=ToolResultStatus.FAILED, retryable=False)

    plan = build_workflow_plan(WorkflowId.WF_001, extraction_tool_ids=())[:1]
    outcome = Orchestrator(
        ToolRegistryAdapter({"TOOL-001": failing}),
        max_attempts=3,
    ).execute(plan, {plan.steps[0].step_id: request})

    assert outcome.succeeded is False
    assert calls == 1


def test_orchestrator_does_not_retry_after_any_reported_side_effect() -> None:
    request = _request()
    calls = 0

    def changed(value: ToolRequest[RegisterWorkspaceInput]) -> ToolResult[WorkspaceDescriptor]:
        nonlocal calls
        calls += 1
        result = _result(value, status=ToolResultStatus.FAILED, retryable=True)
        return result.model_copy(
            update={
                "side_effects": (
                    SideEffectRecord(
                        side_effect_id=uuid4(),
                        kind=SideEffectKind.POLICY_WRITE,
                        target="workspace-policy",
                        description="The workspace policy was recorded.",
                        occurred_at=datetime.now(UTC),
                        idempotency_key=str(value.request_id),
                    ),
                )
            }
        )

    plan = build_workflow_plan(WorkflowId.WF_001, extraction_tool_ids=())[:1]
    outcome = Orchestrator(
        ToolRegistryAdapter({"TOOL-001": changed}),
        max_attempts=3,
    ).execute(plan, {plan.steps[0].step_id: request})

    assert outcome.succeeded is False
    assert calls == 1


def test_agent_roles_and_workflow_plans_cover_epic_008() -> None:
    assert set(AGENT_ROLES) == set(AgentId)
    assert set(WorkflowId) == {
        WorkflowId.WF_001,
        WorkflowId.WF_002,
        WorkflowId.WF_003,
        WorkflowId.WF_004,
        WorkflowId.WF_005,
        WorkflowId.WF_006,
        WorkflowId.WF_007,
    }
    assert build_workflow_plan(
        WorkflowId.WF_001,
        extraction_tool_ids=("TOOL-006", "TOOL-011"),
    ).tool_ids == (
        "TOOL-001",
        "TOOL-002",
        "TOOL-003",
        "TOOL-004",
        "TOOL-006",
        "TOOL-011",
        "TOOL-005",
        "TOOL-022",
    )
    assert build_workflow_plan(WorkflowId.WF_002).tool_ids == (
        "TOOL-014",
        "TOOL-015",
        "TOOL-016",
        "TOOL-023",
    )
    assert build_workflow_plan(WorkflowId.WF_003).tool_ids == ("TOOL-017", "TOOL-024")
    assert build_workflow_plan(WorkflowId.WF_004).tool_ids == (
        "TOOL-018",
        "TOOL-020",
        "TOOL-028",
        "TOOL-018",
        "TOOL-029",
    )
    assert build_workflow_plan(WorkflowId.WF_005).tool_ids == (
        "TOOL-025",
        "TOOL-019",
        "TOOL-020",
        "TOOL-022",
        "TOOL-023",
        "TOOL-027",
    )
    assert build_workflow_plan(WorkflowId.WF_006).tool_ids == (
        "TOOL-028",
        "TOOL-020",
        "TOOL-029",
        "TOOL-030",
    )
    assert build_workflow_plan(WorkflowId.WF_007).tool_ids == ("TOOL-026",)


def test_registry_and_orchestration_contracts_fail_closed() -> None:
    request = _request()

    def mismatched(value: ToolRequest[RegisterWorkspaceInput]) -> ToolResult[WorkspaceDescriptor]:
        return _result(value).model_copy(update={"tool_id": "TOOL-002"})

    with pytest.raises(OrchestrationError, match="UNREGISTERED_TOOL"):
        ToolRegistryAdapter({"TOOL-999": _result})
    with pytest.raises(OrchestrationError, match="TOOL_RESULT_INVALID"):
        ToolRegistryAdapter({"TOOL-001": lambda _request: object()}).invoke(
            "TOOL-001",
            request,
        )
    with pytest.raises(OrchestrationError, match="TOOL_RESULT_INVALID"):
        ToolRegistryAdapter({"TOOL-001": mismatched}).invoke("TOOL-001", request)
    with pytest.raises(ValueError, match="between one and ten"):
        Orchestrator(ToolRegistryAdapter({}), max_attempts=0)

    plan = build_workflow_plan(WorkflowId.WF_001)[:1]
    with pytest.raises(OrchestrationError, match="WORKFLOW_INPUT_MISSING"):
        Orchestrator(ToolRegistryAdapter({})).execute(plan, {})


def test_plan_and_role_definitions_reject_unsafe_capabilities() -> None:
    with pytest.raises(ValueError, match="unregistered"):
        AgentRole(AgentId.AGENT_001, "invalid", frozenset({"TOOL-999"}), (), ())
    with pytest.raises(ValueError, match="human authority"):
        AgentRole(AgentId.AGENT_001, "invalid", frozenset({"TOOL-029"}), (), ())
    with pytest.raises(ValueError, match="registered"):
        WorkflowStep("step-01", "TOOL-999", AgentId.AGENT_001, "Invalid.")
    with pytest.raises(ValueError, match="cannot be assigned"):
        WorkflowStep(
            "step-01",
            "TOOL-001",
            AgentId.AGENT_001,
            "Invalid.",
            authority=ExecutionAuthority.HUMAN,
        )
    with pytest.raises(ValueError, match="not allowed"):
        WorkflowStep("step-01", "TOOL-020", AgentId.AGENT_001, "Invalid.")
    with pytest.raises(ValueError, match="at least one"):
        WorkflowPlan(WorkflowId.WF_001, ())
    duplicate = WorkflowStep("step-01", "TOOL-001", AgentId.AGENT_001, "Register.")
    with pytest.raises(ValueError, match="unique"):
        WorkflowPlan(WorkflowId.WF_001, (duplicate, duplicate))
    with pytest.raises(TypeError, match="slicing"):
        build_workflow_plan(WorkflowId.WF_001)[0]  # type: ignore[index]
    with pytest.raises(ValueError, match="only for WF-001"):
        build_workflow_plan(WorkflowId.WF_002, extraction_tool_ids=("TOOL-006",))
    with pytest.raises(ValueError, match="TOOL-006 through TOOL-013"):
        build_workflow_plan(WorkflowId.WF_001, extraction_tool_ids=("TOOL-014",))


def test_optional_workflow_steps_can_be_omitted_without_side_effects() -> None:
    write_step = build_workflow_plan(WorkflowId.WF_004)[3:4]

    outcome = Orchestrator(ToolRegistryAdapter({})).execute(write_step, {})

    assert outcome.succeeded is True
    assert outcome.steps == ()
    assert outcome.narrative == ()
