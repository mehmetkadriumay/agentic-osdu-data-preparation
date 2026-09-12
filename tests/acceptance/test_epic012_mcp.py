from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from agentic_osdu.api.app import create_app
from agentic_osdu.domain.models import (
    EvidenceRecord,
    ProvenanceRecord,
    TrustLevel,
    WorkspaceRelativePath,
)
from agentic_osdu.mcp_server import create_mcp_server, mcp_tool_name
from agentic_osdu.runtime import create_runtime
from agentic_osdu.tools.contracts import (
    TOOL_REGISTRY,
    RegisterWorkspaceInput,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
    WorkspaceDescriptor,
)


class RecordingInvoker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ToolRequest[Any]]] = []

    def invoke(self, tool_id: str, request: ToolRequest[Any]) -> ToolResult[Any]:
        self.calls.append((tool_id, request))
        now = datetime.now(UTC)
        return ToolResult[WorkspaceDescriptor](
            request_id=request.request_id,
            tool_id=tool_id,
            tool_version=TOOL_REGISTRY[tool_id].version,
            status=ToolResultStatus.SUCCEEDED,
            output=WorkspaceDescriptor(
                workspace_id=request.workspace_id,
                canonical_root=request.input.root_path,
                read_only=True,
                allowed_output_subpaths=request.input.allowed_output_subpaths,
                policy_fingerprint="a" * 64,
            ),
            provenance=(
                ProvenanceRecord(
                    provenance_id=uuid4(),
                    source_type="test",
                    source_ref=tool_id,
                    tool_id=tool_id,
                    tool_version=TOOL_REGISTRY[tool_id].version,
                    recorded_at=now,
                ),
            ),
            evidence=(
                EvidenceRecord(
                    evidence_id=uuid4(),
                    evidence_type="test",
                    rule_id="EPIC-012",
                    summary="The registered tool was invoked.",
                    trust_level=TrustLevel.VERIFIED,
                ),
            ),
            trust_level=TrustLevel.VERIFIED,
            started_at=now,
            finished_at=now,
        )


def _workspace_request() -> dict[str, object]:
    return {
        "request_id": str(uuid4()),
        "workspace_id": str(uuid4()),
        "actor": {"actor_id": "human.operator"},
        "input": {
            "root_path": r"C:\Approved",
            "read_only": True,
            "allowed_output_subpaths": ["generated"],
        },
    }


def _rpc(method: str, *, request_id: int | None = None, params: object = None) -> dict[str, object]:
    message: dict[str, object] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return message


def test_mcp_enumerates_all_registered_tools_with_registry_metadata_and_schemas() -> None:
    server = create_mcp_server(RecordingInvoker())
    tools = __import__("asyncio").run(server.list_tools())

    assert len(tools) == 30
    assert [tool.name for tool in tools] == [
        mcp_tool_name(definition) for definition in TOOL_REGISTRY.values()
    ]
    for tool, definition in zip(tools, TOOL_REGISTRY.values(), strict=True):
        assert definition.tool_id in tool.title
        assert definition.purpose in tool.description
        request_type = ToolRequest[definition.input_model]  # type: ignore[name-defined]
        assert (
            tool.input_schema["properties"]["input"]
            == request_type.model_json_schema()["properties"]["input"]
        )
        assert tool.output_schema is not None
        assert "errors" in json.dumps(tool.output_schema)


def test_mcp_dispatches_through_the_existing_typed_registry() -> None:
    invoker = RecordingInvoker()
    server = create_mcp_server(invoker)

    result = __import__("asyncio").run(
        server.call_tool(mcp_tool_name(TOOL_REGISTRY["TOOL-001"]), _workspace_request())
    )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["tool_id"] == "TOOL-001"
    assert invoker.calls[0][0] == "TOOL-001"
    assert isinstance(invoker.calls[0][1].input, RegisterWorkspaceInput)
    assert invoker.calls[0][1].input.allowed_output_subpaths == (
        WorkspaceRelativePath("generated"),
    )


def test_production_runtime_exposes_every_registered_tool(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("AGENTIC_OSDU_STATE_PATH", str(tmp_path / "runtime-state.db"))
    runtime = create_runtime()
    try:
        assert runtime.registry.executable_tool_ids == frozenset(TOOL_REGISTRY)
    finally:
        runtime.database.dispose()


def test_streamable_http_supports_initialize_list_and_call() -> None:
    invoker = RecordingInvoker()
    server = create_mcp_server(invoker)
    app = create_app(invoker, mcp_server=server)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        initialized = client.post(
            "/mcp/",
            headers=headers,
            json=_rpc(
                "initialize",
                request_id=1,
                params={
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "epic-012-test", "version": "1.0"},
                },
            ),
        )
        assert initialized.status_code == 200
        session_id = initialized.headers["mcp-session-id"]
        session_headers = {**headers, "Mcp-Session-Id": session_id}
        ready = client.post(
            "/mcp/",
            headers=session_headers,
            json=_rpc("notifications/initialized"),
        )
        assert ready.status_code in {200, 202}

        listed = client.post(
            "/mcp/",
            headers=session_headers,
            json=_rpc("tools/list", request_id=2, params={}),
        )
        assert listed.status_code == 200
        assert len(listed.json()["result"]["tools"]) == 30

        called = client.post(
            "/mcp/",
            headers=session_headers,
            json=_rpc(
                "tools/call",
                request_id=3,
                params={
                    "name": mcp_tool_name(TOOL_REGISTRY["TOOL-001"]),
                    "arguments": _workspace_request(),
                },
            ),
        )
        assert called.status_code == 200
        result = called.json()["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["tool_id"] == "TOOL-001"

        denied_learning = client.post(
            "/mcp/",
            headers=session_headers,
            json=_rpc(
                "tools/call",
                request_id=4,
                params={
                    "name": mcp_tool_name(TOOL_REGISTRY["TOOL-024"]),
                    "arguments": {
                        **_workspace_request(),
                        "input": {"mutation": {"action": "clear"}},
                    },
                },
            ),
        )
        assert denied_learning.status_code == 200
        denied_result = denied_learning.json()["result"]
        assert denied_result["isError"] is True
        assert denied_result["structuredContent"]["errors"][0]["code"] == (
            "HUMAN_APPROVAL_REQUIRED"
        )
        assert [tool_id for tool_id, _request in invoker.calls] == ["TOOL-001"]


def test_stdio_entrypoint_starts_and_speaks_mcp_protocol(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["AGENTIC_OSDU_STATE_PATH"] = str(tmp_path / "stdio-state.db")
    process = subprocess.Popen(
        [sys.executable, "-m", "agentic_osdu.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        process.stdin.write(
            json.dumps(
                _rpc(
                    "initialize",
                    request_id=1,
                    params={
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "stdio-test", "version": "1.0"},
                    },
                )
            )
            + "\n"
        )
        process.stdin.flush()
        initialized = json.loads(process.stdout.readline())
        assert initialized["id"] == 1
        assert initialized["result"]["serverInfo"]["name"] == "agentic-osdu-data-preparation"

        process.stdin.write(json.dumps(_rpc("notifications/initialized")) + "\n")
        process.stdin.write(json.dumps(_rpc("tools/list", request_id=2, params={})) + "\n")
        process.stdin.flush()
        listed = json.loads(process.stdout.readline())
        assert listed["id"] == 2
        assert len(listed["result"]["tools"]) == 30
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_packaging_and_repository_copilot_configuration_publish_the_stdio_server() -> None:
    root = Path(__file__).resolve().parents[2]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["scripts"]["agentic-osdu-mcp"] == (
        "agentic_osdu.mcp_server:entrypoint"
    )
    assert "mcp==2.2.0" in metadata["project"]["dependencies"]

    configuration = json.loads((root / ".github" / "mcp.json").read_text(encoding="utf-8"))
    server = configuration["mcpServers"]["agentic-osdu-data-preparation"]
    assert server == {
        "type": "stdio",
        "command": "agentic-osdu-mcp",
        "args": [],
        "tools": ["*"],
    }


def test_uv_lock_contains_only_portable_registry_artifacts() -> None:
    root = Path(__file__).resolve().parents[2]
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))

    def is_absolute_local(value: str) -> bool:
        return value.startswith(("/", "\\\\", "file:")) or (
            len(value) > 2 and value[1] == ":" and value[2] in {"/", "\\"}
        )

    for package in lock["package"]:
        source = package.get("source", {})
        assert not any(
            is_absolute_local(value) for value in source.values() if isinstance(value, str)
        )
        if registry := source.get("registry"):
            assert registry.startswith(("https://", "http://"))
            artifacts = [*package.get("wheels", [])]
            if sdist := package.get("sdist"):
                artifacts.append(sdist)
            assert artifacts
            assert all(
                artifact["url"].startswith(("https://", "http://")) for artifact in artifacts
            )
            assert all(artifact["hash"].startswith("sha256:") for artifact in artifacts)
        artifacts = [*package.get("wheels", [])]
        if sdist := package.get("sdist"):
            artifacts.append(sdist)
        for artifact in artifacts:
            assert "path" not in artifact
