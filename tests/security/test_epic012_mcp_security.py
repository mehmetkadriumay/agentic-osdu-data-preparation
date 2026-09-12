from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp_types import CallToolResult

from agentic_osdu.agents.orchestrator import OrchestrationError
from agentic_osdu.mcp_server import create_mcp_server, mcp_tool_name
from agentic_osdu.tools.contracts import TOOL_REGISTRY, ToolRequest, ToolResult


class BoundaryInvoker:
    def invoke(self, tool_id: str, request: ToolRequest[Any]) -> ToolResult[Any]:
        del tool_id, request
        raise OrchestrationError(
            "ROOT_POLICY_DENIED",
            r"The path C:\Sensitive\private-data must not be disclosed.",
        )


class UnexpectedFailureInvoker:
    def invoke(self, tool_id: str, request: ToolRequest[Any]) -> ToolResult[Any]:
        del tool_id, request
        raise RuntimeError(r"database password and C:\Sensitive\state.db")


def _request(input_value: dict[str, object]) -> dict[str, object]:
    return {
        "request_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "actor": {"actor_id": "human.operator"},
        "input": input_value,
    }


def _call(tool_id: str, invoker: Any, input_value: dict[str, object]) -> CallToolResult:
    result = asyncio.run(
        create_mcp_server(invoker).call_tool(
            mcp_tool_name(TOOL_REGISTRY[tool_id]),
            _request(input_value),
        )
    )
    assert isinstance(result, CallToolResult)
    return result


def test_mcp_maps_expected_and_unexpected_failures_without_sensitive_details() -> None:
    expected = _call(
        "TOOL-001",
        BoundaryInvoker(),
        {
            "root_path": r"C:\Approved",
            "read_only": True,
            "allowed_output_subpaths": [],
        },
    )
    assert expected.is_error is True
    assert expected.structured_content is not None
    assert expected.structured_content["errors"][0]["code"] == "ROOT_POLICY_DENIED"
    assert "Sensitive" not in str(expected.structured_content)

    unexpected = _call(
        "TOOL-001",
        UnexpectedFailureInvoker(),
        {
            "root_path": r"C:\Approved",
            "read_only": True,
            "allowed_output_subpaths": [],
        },
    )
    assert unexpected.is_error is True
    assert unexpected.structured_content is not None
    assert unexpected.structured_content["errors"][0]["code"] == "TOOL_EXECUTION_FAILED"
    assert "password" not in str(unexpected.structured_content)
    assert "Sensitive" not in str(unexpected.structured_content)


def test_mcp_cannot_bypass_generation_or_network_approval_boundaries() -> None:
    generated = _call(
        "TOOL-018",
        BoundaryInvoker(),
        {
            "file_id": str(uuid4()),
            "learning_model_id": str(uuid4()),
            "generation_policy_version": "1.0.0",
            "dry_run": False,
        },
    )
    assert generated.is_error is True
    assert generated.structured_content is not None
    assert generated.structured_content["errors"][0]["code"] == "HUMAN_APPROVAL_REQUIRED"

    refreshed = _call(
        "TOOL-021",
        BoundaryInvoker(),
        {
            "source": "approved_remote",
            "revision": "v1",
            "remote_uri": "https://schemas.example.test/catalog",
            "expected_checksums": [{"relative_path": "schemas/example.json", "sha256": "a" * 64}],
            "network_approval_id": str(uuid4()),
        },
    )
    assert refreshed.is_error is True
    assert refreshed.structured_content is not None
    assert refreshed.structured_content["errors"][0]["code"] == "NETWORK_NOT_APPROVED"

    tracked_generation = _call(
        "TOOL-025",
        BoundaryInvoker(),
        {
            "definition": {
                "job_type": "generate-all-manifests",
                "steps": [{"sequence": 1, "tool_id": "TOOL-020", "input_ref": "generation"}],
            },
            "deduplication_key": "epic-012-generation-boundary",
            "workflow_id": "WF-005",
            "generation": {
                "inventory_id": str(uuid4()),
                "schema_catalog_id": str(uuid4()),
                "generation_policy_version": "1.0.0",
                "dry_run": False,
            },
        },
    )
    assert tracked_generation.is_error is True
    assert tracked_generation.structured_content is not None
    assert tracked_generation.structured_content["errors"][0]["code"] == ("HUMAN_APPROVAL_REQUIRED")

    decision = _call(
        "TOOL-029",
        BoundaryInvoker(),
        {
            "decision": {
                "decision_id": str(uuid4()),
                "actor": {"actor_id": "claimed.human"},
                "target_type": "generated_candidate",
                "target_id": str(uuid4()),
                "target_version": "1",
                "decision": "approve",
                "reason": "claimed approval",
                "decided_at": "2026-09-11T22:00:00Z",
            }
        },
    )
    assert decision.is_error is True
    assert decision.structured_content is not None
    assert decision.structured_content["errors"][0]["code"] == "HUMAN_APPROVAL_REQUIRED"


def test_mcp_surface_adds_no_shell_filesystem_or_osdu_ingestion_tools() -> None:
    tools = asyncio.run(create_mcp_server(BoundaryInvoker()).list_tools())
    names = {tool.name.casefold() for tool in tools}
    assert len(names) == 30
    assert not any(
        prohibited in name
        for name in names
        for prohibited in ("shell", "command", "filesystem", "arbitrary-file", "ingest")
    )


def test_mcp_adapter_contains_no_domain_business_logic_duplication() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "src" / "agentic_osdu" / "mcp_server.py"
    ).read_text(encoding="utf-8")
    assert "create_runtime" in source
    assert "invoker.invoke(definition.tool_id, request)" in source
    for forbidden_import in (
        "agentic_osdu.formats",
        "agentic_osdu.manifests",
        "agentic_osdu.schemas",
        "agentic_osdu.state",
        "agentic_osdu.tools.discovery",
        "agentic_osdu.tools.detection",
        "agentic_osdu.tools.review",
    ):
        assert forbidden_import not in source
