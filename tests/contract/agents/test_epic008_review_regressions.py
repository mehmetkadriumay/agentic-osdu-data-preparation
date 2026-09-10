from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from agentic_osdu.agents.orchestrator import (
    OrchestrationError,
    Orchestrator,
    ToolRegistryAdapter,
)
from agentic_osdu.agents.roles import AGENT_ROLES, AgentId
from agentic_osdu.agents.workflows import (
    WorkflowId,
    WorkflowPlan,
    WorkflowStep,
    build_workflow_plan,
)
from agentic_osdu.domain.models import (
    ActorRef,
    EvidenceRecord,
    FileAssetRef,
    GeneratedCandidateRef,
    GeneratedManifestCandidate,
    ProvenanceRecord,
    TrustLevel,
    WorkspaceRelativePath,
)
from agentic_osdu.tools.contracts import (
    BuildManifestReviewInput,
    DiscoverFilesInput,
    DiscoveryBatch,
    DiscoveryOutput,
    FileSampleOutput,
    GenerateManifestInput,
    GenerateManifestOutput,
    ReadFileSampleInput,
    SampleMode,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
)


def _request[InputT: BaseModel](
    input_value: InputT,
    actor: AgentId,
    *,
    workspace_id: UUID | None = None,
) -> ToolRequest[InputT]:
    return ToolRequest.model_construct(
        request_id=uuid4(),
        workspace_id=workspace_id or uuid4(),
        actor=ActorRef(actor_id=actor.value),
        input=input_value,
        cancellation_token_id=None,
        expected_state_version=None,
    )


def _unchecked_result[OutputT: BaseModel](
    tool_id: str,
    request: ToolRequest[Any],
    output: OutputT,
    trust: TrustLevel,
) -> ToolResult[OutputT]:
    now = datetime.now(UTC)
    return ToolResult.model_construct(
        request_id=request.request_id,
        tool_id=tool_id,
        tool_version="1.0.0",
        status=ToolResultStatus.SUCCEEDED,
        output=output,
        errors=(),
        provenance=(
            ProvenanceRecord(
                provenance_id=uuid4(),
                source_type="test_fixture",
                source_ref="bounded",
                tool_id=tool_id,
                tool_version="1.0.0",
                recorded_at=now,
            ),
        ),
        evidence=(
            EvidenceRecord(
                evidence_id=uuid4(),
                evidence_type="test_fixture",
                rule_id="TEST-EPIC-008",
                summary="Bounded regression fixture.",
                trust_level=trust,
            ),
        ),
        trust_level=trust,
        side_effects=(),
        started_at=now,
        finished_at=now,
    )


def test_wf004_dry_run_step_rejects_write_input_even_without_approval() -> None:
    step = build_workflow_plan(WorkflowId.WF_004)[:1]
    request = _request(
        GenerateManifestInput(
            file_id=uuid4(),
            learning_model_id=uuid4(),
            generation_policy_version="1.0.0",
            dry_run=False,
        ),
        AgentId.MANIFEST_GENERATION,
    )

    with pytest.raises(OrchestrationError, match="STEP_INPUT_POLICY_DENIED"):
        Orchestrator(ToolRegistryAdapter({})).execute(step, {"step-01": request})


def test_human_approval_receipts_reject_self_assertion_replay_and_payload_mismatch() -> None:
    from agentic_osdu.agents.orchestrator import ApprovalReceipt, HumanApprovalBoundary

    now = datetime.now(UTC)

    def clock() -> datetime:
        return now

    authority = HumanApprovalBoundary(b"trusted-boundary-secret", clock=clock)
    plan = build_workflow_plan(WorkflowId.WF_004)[3:4]
    step = plan.steps[0]
    run_id = uuid4()
    request = _request(
        GenerateManifestInput(
            file_id=uuid4(),
            learning_model_id=uuid4(),
            generation_policy_version="1.0.0",
            dry_run=False,
        ),
        AgentId.MANIFEST_GENERATION,
    )
    receipt = authority.issue(
        workflow_id=plan.workflow_id,
        run_id=run_id,
        step=step,
        request=request,
        approved_by=ActorRef(actor_id="human-reviewer"),
        expires_at=now + timedelta(minutes=5),
    )
    forged = ApprovalReceipt(
        receipt_id=uuid4(),
        workflow_id=receipt.workflow_id,
        run_id=receipt.run_id,
        workspace_id=receipt.workspace_id,
        action=receipt.action,
        request_id=receipt.request_id,
        target_identity=receipt.target_identity,
        payload_sha256=receipt.payload_sha256,
        approved_by=receipt.approved_by,
        issued_at=receipt.issued_at,
        expires_at=receipt.expires_at,
        signature="0" * 64,
    )

    with pytest.raises(OrchestrationError, match="APPROVAL_RECEIPT_INVALID"):
        authority.authorize(forged, plan.workflow_id, run_id, step, request)
    with pytest.raises(OrchestrationError, match="SELF_APPROVAL_DENIED"):
        authority.issue(
            workflow_id=plan.workflow_id,
            run_id=run_id,
            step=step,
            request=request,
            approved_by=ActorRef(actor_id=AgentId.MANIFEST_GENERATION.value),
            expires_at=now + timedelta(minutes=5),
        )

    authority.authorize(receipt, plan.workflow_id, run_id, step, request)
    authority.authorize(receipt, plan.workflow_id, run_id, step, request)  # exact replay is safe
    changed_payload = request.model_copy(
        update={"input": request.input.model_copy(update={"dry_run": True})}
    )
    with pytest.raises(OrchestrationError, match="APPROVAL_BINDING_MISMATCH"):
        authority.authorize(receipt, plan.workflow_id, run_id, step, changed_payload)
    changed_target = request.model_copy(
        update={"input": request.input.model_copy(update={"learning_model_id": uuid4()})}
    )
    with pytest.raises(OrchestrationError, match="APPROVAL_BINDING_MISMATCH"):
        authority.authorize(receipt, plan.workflow_id, run_id, step, changed_target)
    with pytest.raises(OrchestrationError, match="APPROVAL_BINDING_MISMATCH"):
        authority.authorize(receipt, plan.workflow_id, uuid4(), step, request)
    expired_authority = HumanApprovalBoundary(
        b"trusted-boundary-secret",
        clock=lambda: now + timedelta(minutes=10),
    )
    with pytest.raises(OrchestrationError, match="APPROVAL_RECEIPT_EXPIRED"):
        expired_authority.authorize(receipt, plan.workflow_id, run_id, step, request)
    with pytest.raises(OrchestrationError, match="UNREGISTERED_TOOL"):
        Orchestrator(
            ToolRegistryAdapter({}),
            approval_boundary=authority,
        ).execute(
            plan,
            {"step-04": request},
            run_id=run_id,
            approval_receipts={"step-04": receipt},
        )


@pytest.mark.parametrize(
    ("expected_role", "actual_role"),
    [(expected, actual) for expected in AgentId for actual in AgentId if expected is not actual],
)
def test_executor_denies_every_cross_role_agent_invocation(
    expected_role: AgentId,
    actual_role: AgentId,
) -> None:
    tool_id = sorted(AGENT_ROLES[expected_role].allowed_tool_ids)[0]
    step = WorkflowStep("step-01", tool_id, expected_role, "Role boundary regression.")
    plan = WorkflowPlan(WorkflowId.WF_001, (step,))
    request = _request(DiscoverFilesInput(workspace_id=uuid4()), actual_role)

    with pytest.raises(OrchestrationError, match="AGENT_ROLE_MISMATCH"):
        Orchestrator(ToolRegistryAdapter({})).execute(plan, {"step-01": request})


def test_registry_rejects_fixed_generated_and_inherited_trust_escalation() -> None:
    fixed_request = _request(
        GenerateManifestInput(
            file_id=uuid4(),
            learning_model_id=uuid4(),
            generation_policy_version="1.0.0",
        ),
        AgentId.MANIFEST_GENERATION,
    )
    fixed_output = GenerateManifestOutput.model_construct()  # type: ignore[call-arg]
    fixed_result = _unchecked_result(
        "TOOL-018",
        fixed_request,
        fixed_output,
        TrustLevel.AUTHORITATIVE,
    )
    with pytest.raises(OrchestrationError, match="TOOL_TRUST_POLICY_VIOLATION"):
        ToolRegistryAdapter({"TOOL-018": lambda _request: fixed_result}).invoke(
            "TOOL-018", fixed_request
        )

    reference = GeneratedCandidateRef.model_construct(  # type: ignore[call-arg]
        trust_level=TrustLevel.HEURISTIC
    )
    heuristic_manifest = GeneratedManifestCandidate.model_construct(
        reference=reference,
    )  # type: ignore[call-arg]
    inherited_input = BuildManifestReviewInput.model_construct(manifest=heuristic_manifest)
    inherited_request = _request(inherited_input, AgentId.VALIDATION_REVIEW)
    inherited_output = __import__(
        "agentic_osdu.tools.contracts", fromlist=["ManifestReviewView"]
    ).ManifestReviewView.model_construct(manifest=heuristic_manifest)
    inherited_result = _unchecked_result(
        "TOOL-028",
        inherited_request,
        inherited_output,
        TrustLevel.VERIFIED,
    )
    with pytest.raises(OrchestrationError, match="TOOL_TRUST_POLICY_VIOLATION"):
        ToolRegistryAdapter({"TOOL-028": lambda _request: inherited_result}).invoke(
            "TOOL-028", inherited_request
        )


def test_executor_fans_out_derived_requests_in_stable_order_and_joins_results() -> None:
    from agentic_osdu.agents.orchestrator import BoundStepRequest
    from agentic_osdu.agents.workflows import StepCardinality

    workspace_id = uuid4()
    files = tuple(
        FileAssetRef(
            file_id=uuid4(),
            workspace_id=workspace_id,
            relative_path=WorkspaceRelativePath(path),
            size_bytes=4,
            modified_at=datetime.now(UTC),
            discovery_version=1,
        )
        for path in ("a.las", "b.csv")
    )
    discovery_request = _request(
        DiscoverFilesInput(workspace_id=workspace_id),
        AgentId.WORKSPACE_INTAKE,
        workspace_id=workspace_id,
    )

    def discover(request: ToolRequest[DiscoverFilesInput]) -> ToolResult[DiscoveryOutput]:
        output = DiscoveryOutput(
            batch=DiscoveryBatch(
                discovery_id=uuid4(),
                workspace_id=workspace_id,
                file_count=2,
                total_bytes=8,
                snapshot_at=datetime.now(UTC),
            ),
            files=files,
        )
        return _unchecked_result("TOOL-002", request, output, TrustLevel.VERIFIED)

    seen: list[UUID] = []
    joined_counts: list[int] = []

    def sample(request: ToolRequest[ReadFileSampleInput]) -> ToolResult[FileSampleOutput]:
        seen.append(request.input.file_id)
        output = FileSampleOutput.model_construct()  # type: ignore[call-arg]
        return _unchecked_result("TOOL-003", request, output, TrustLevel.VERIFIED)

    plan = WorkflowPlan(
        WorkflowId.WF_001,
        (
            WorkflowStep("discover", "TOOL-002", AgentId.WORKSPACE_INTAKE, "Discover."),
            WorkflowStep(
                "sample",
                "TOOL-003",
                AgentId.WORKSPACE_INTAKE,
                "Sample each file.",
                depends_on=("discover",),
                cardinality=StepCardinality.FAN_OUT,
            ),
            WorkflowStep(
                "join",
                "TOOL-002",
                AgentId.WORKSPACE_INTAKE,
                "Join sampled files.",
                depends_on=("sample",),
                cardinality=StepCardinality.JOIN,
            ),
        ),
    )

    def derive_samples(context: Any) -> tuple[BoundStepRequest, ...]:
        discovered = context.dependencies[0].result.output.files
        return tuple(
            BoundStepRequest(
                binding_key=str(item.file_id),
                request=_request(
                    ReadFileSampleInput(
                        file_id=item.file_id,
                        mode=SampleMode.PREFIX,
                        max_bytes=4,
                    ),
                    AgentId.WORKSPACE_INTAKE,
                    workspace_id=workspace_id,
                ),
            )
            for item in discovered
        )

    def derive_join(context: Any) -> tuple[BoundStepRequest, ...]:
        joined_counts.append(len(context.dependencies))
        return (BoundStepRequest("joined", discovery_request),)

    outcome = Orchestrator(ToolRegistryAdapter({"TOOL-002": discover, "TOOL-003": sample})).execute(
        plan,
        {"discover": discovery_request},
        request_derivers={"sample": derive_samples, "join": derive_join},
    )

    assert outcome.succeeded is True
    assert seen == [item.file_id for item in files]
    assert joined_counts == [2]
    assert [item.binding_key for item in outcome.steps] == [
        "discover",
        str(files[0].file_id),
        str(files[1].file_id),
        "joined",
    ]
    for workflow_id in (WorkflowId.WF_001, WorkflowId.WF_002, WorkflowId.WF_005):
        workflow = build_workflow_plan(workflow_id)
        assert any(step.cardinality is StepCardinality.FAN_OUT for step in workflow.steps)
        assert any(len(step.depends_on) > 1 for step in workflow.steps)
