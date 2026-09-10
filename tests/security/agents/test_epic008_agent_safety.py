from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentic_osdu.agents.orchestrator import OrchestrationError, Orchestrator, ToolRegistryAdapter
from agentic_osdu.agents.roles import AGENT_ROLES, AgentId
from agentic_osdu.agents.workflows import ExecutionAuthority, WorkflowId, build_workflow_plan
from agentic_osdu.domain.models import ActorRef
from agentic_osdu.tools.contracts import (
    GenerateManifestInput,
    ParseManifestsInput,
    RegisterWorkspaceInput,
    ToolRequest,
)


def test_agents_never_import_domain_implementations() -> None:
    agents_root = Path(__file__).parents[3] / "src" / "agentic_osdu" / "agents"
    prohibited = {
        "agentic_osdu.formats",
        "agentic_osdu.jobs",
        "agentic_osdu.manifests",
        "agentic_osdu.schemas",
        "agentic_osdu.state",
        "agentic_osdu.tools.detection",
        "agentic_osdu.tools.discovery",
        "agentic_osdu.tools.review",
    }
    imported: set[str] = set()
    for source_path in agents_root.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
    assert not {
        module
        for module in imported
        if any(module == denied or module.startswith(f"{denied}.") for denied in prohibited)
    }


def test_agents_cannot_select_unregistered_or_prohibited_tools() -> None:
    for role in AGENT_ROLES.values():
        assert role.allowed_tool_ids
        assert all(tool_id.startswith("TOOL-") for tool_id in role.allowed_tool_ids)
        assert "OSDU-INGEST" not in role.allowed_tool_ids
    with pytest.raises(OrchestrationError, match="UNREGISTERED_TOOL"):
        ToolRegistryAdapter({}).definition("OSDU-INGEST")
    with pytest.raises(ValueError, match="registered"):
        build_workflow_plan(WorkflowId.WF_001, extraction_tool_ids=("TOOL-999",))


def test_only_user_authority_can_approve_or_write_candidates() -> None:
    generation = build_workflow_plan(WorkflowId.WF_004)
    review = build_workflow_plan(WorkflowId.WF_006)

    assert generation.steps[-1].tool_id == "TOOL-029"
    assert generation.steps[-1].authority is ExecutionAuthority.HUMAN
    assert generation.steps[-2].requires_approval is True
    assert review.steps[-2].authority is ExecutionAuthority.HUMAN
    assert review.steps[-1].requires_approval is True
    assert "TOOL-029" not in AGENT_ROLES[AgentId.AGENT_005].allowed_tool_ids


def test_non_intake_requests_cannot_carry_raw_absolute_paths() -> None:
    with pytest.raises(ValidationError, match="workspace-relative"):
        ParseManifestsInput(
            workspace_id=uuid4(),
            manifest_root=r"C:\Approved\Manifests",
        )


def test_agent_cannot_execute_a_human_review_decision() -> None:
    plan = build_workflow_plan(WorkflowId.WF_004)[-1:]
    request = ToolRequest[RegisterWorkspaceInput](
        request_id=uuid4(),
        workspace_id=uuid4(),
        actor=ActorRef(actor_id=AgentId.AGENT_005.value),
        input=RegisterWorkspaceInput(root_path=r"C:\Approved"),
    )
    orchestrator = Orchestrator(ToolRegistryAdapter({}))
    with pytest.raises(OrchestrationError, match="SELF_APPROVAL_DENIED"):
        orchestrator.execute(
            plan,
            {plan.steps[0].step_id: request},
        )


def test_approval_gated_steps_do_not_run_without_user_approval() -> None:
    plan = build_workflow_plan(WorkflowId.WF_004)[3:4]
    request = ToolRequest[GenerateManifestInput](
        request_id=uuid4(),
        workspace_id=uuid4(),
        actor=ActorRef(actor_id=AgentId.MANIFEST_GENERATION.value),
        input=GenerateManifestInput(
            file_id=uuid4(),
            learning_model_id=uuid4(),
            generation_policy_version="1.0.0",
            dry_run=False,
        ),
    )
    with pytest.raises(OrchestrationError, match="HUMAN_APPROVAL_REQUIRED"):
        Orchestrator(ToolRegistryAdapter({})).execute(
            plan,
            {plan.steps[0].step_id: request},
        )
