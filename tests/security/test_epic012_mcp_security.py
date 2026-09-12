from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from mcp_types import CallToolResult

from agentic_osdu.agents.orchestrator import OrchestrationError
from agentic_osdu.domain.models import ActorRef
from agentic_osdu.mcp_server import create_mcp_server, mcp_tool_name
from agentic_osdu.runtime import create_runtime
from agentic_osdu.tools.contracts import (
    TOOL_REGISTRY,
    RegisterWorkspaceInput,
    ToolRequest,
    ToolResult,
)


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


class NoInvocationInvoker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke(self, tool_id: str, request: ToolRequest[Any]) -> ToolResult[Any]:
        del request
        self.calls.append(tool_id)
        raise AssertionError("The approval boundary must reject before registry dispatch.")


def _request(
    input_value: dict[str, object],
    *,
    workspace_id: object | None = None,
) -> dict[str, object]:
    return {
        "request_id": str(uuid4()),
        "workspace_id": str(workspace_id or uuid4()),
        "actor": {"actor_id": "human.operator"},
        "input": input_value,
    }


def _call(
    tool_id: str,
    invoker: Any,
    input_value: dict[str, object],
    *,
    workspace_id: object | None = None,
) -> CallToolResult:
    result = asyncio.run(
        create_mcp_server(invoker).call_tool(
            mcp_tool_name(TOOL_REGISTRY[tool_id]),
            _request(input_value, workspace_id=workspace_id),
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


def _learning_model_payload() -> dict[str, object]:
    content = {"kind": "osdu:wks:Manifest:1.0.0", "Data": {}}
    digest = sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "learning_model_id": str(uuid4()),
        "category": "seismic",
        "version": 1,
        "model_sha256": "d" * 64,
        "example_ids": [],
        "example_identities": [],
        "prototype": {"sha256": digest, "content": content},
        "constants": [],
        "prototype_source_path": "Data/source.sgy",
        "work_product_envelope": {},
        "component_envelope": {},
        "dataset_envelope": {},
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"action": "create", "model": _learning_model_payload(), "expected_version": 0},
        {"action": "activate", "learning_model_id": str(uuid4()), "expected_version": 1},
        {"action": "deactivate", "learning_model_id": str(uuid4()), "expected_version": 1},
        {"action": "clear"},
    ],
)
def test_mcp_rejects_every_direct_learning_model_mutation_before_registry_dispatch(
    mutation: dict[str, object],
) -> None:
    invoker = NoInvocationInvoker()

    result = _call("TOOL-024", invoker, {"mutation": mutation})

    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["errors"][0]["code"] == "HUMAN_APPROVAL_REQUIRED"
    assert invoker.calls == []


def test_mcp_local_schema_import_requires_the_workspace_approved_root(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    approved_catalog = workspace_root / "catalog"
    outside_catalog = tmp_path / "outside"
    relative = Path("manifest") / "Manifest.1.0.0.json"
    payload = b'{"schema":{"type":"object"}}'
    for root in (approved_catalog, outside_catalog):
        target = root / relative
        target.parent.mkdir(parents=True)
        target.write_bytes(payload)

    runtime = create_runtime(tmp_path / "state.db")
    try:
        registered = runtime.registry.invoke(
            "TOOL-001",
            ToolRequest[RegisterWorkspaceInput](
                request_id=uuid4(),
                workspace_id=uuid4(),
                actor=ActorRef(actor_id="human.operator"),
                input=RegisterWorkspaceInput(root_path=str(workspace_root)),
            ),
        )
        assert registered.output is not None
        workspace_id = registered.output.workspace_id
        common_input = {
            "source": "local_export",
            "revision": "epic-012-root-policy",
            "expected_checksums": [
                {
                    "relative_path": relative.as_posix(),
                    "sha256": sha256(payload).hexdigest(),
                }
            ],
        }

        denied = _call(
            "TOOL-021",
            runtime.registry,
            {**common_input, "local_root": str(outside_catalog)},
            workspace_id=workspace_id,
        )
        assert denied.is_error is True
        assert denied.structured_content is not None
        assert denied.structured_content["errors"][0]["code"] == "ROOT_POLICY_DENIED"
        assert str(outside_catalog) not in str(denied.structured_content)

        approved = _call(
            "TOOL-021",
            runtime.registry,
            {**common_input, "local_root": str(approved_catalog)},
            workspace_id=workspace_id,
        )
        assert approved.is_error is False
        assert approved.structured_content is not None
        assert approved.structured_content["status"] == "succeeded"
    finally:
        runtime.database.dispose()


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
