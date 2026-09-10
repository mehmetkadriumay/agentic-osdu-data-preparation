from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentic_osdu.agents.orchestrator import OrchestrationError
from agentic_osdu.api.app import create_app


class RejectingInvoker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def invoke(self, tool_id: str, request: Any) -> Any:
        self.calls.append((tool_id, request))
        raise OrchestrationError("TEST_REJECTION", "Safe test rejection.")


def test_openapi_exposes_complete_epic009_surface_without_ingestion() -> None:
    schema = create_app(RejectingInvoker()).openapi()
    paths = set(schema["paths"])
    assert {
        "/api/v1/workspaces",
        "/api/v1/inventories/discover",
        "/api/v1/inventories/classify",
        "/api/v1/inventories/reset",
        "/api/v1/jobs",
        "/api/v1/jobs/control",
        "/api/v1/jobs/events",
        "/api/v1/jobs/cancel",
        "/api/v1/manifests",
        "/api/v1/associations/match",
        "/api/v1/associations",
        "/api/v1/learning/learn",
        "/api/v1/learning/models",
        "/api/v1/generation/one",
        "/api/v1/generation/all",
        "/api/v1/validation",
        "/api/v1/review/inventories",
        "/api/v1/review/manifests",
        "/api/v1/review/decisions",
        "/api/v1/exports",
    } <= paths
    assert not any("ingest" in path for path in paths)


def test_every_operation_has_stable_id_and_tool_error_response() -> None:
    schema = create_app(RejectingInvoker()).openapi()
    operation_ids: list[str] = []
    for path_item in schema["paths"].values():
        for operation in path_item.values():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            operation_ids.append(operation["operationId"])
            responses = operation["responses"]
            assert {"400", "403", "404", "409", "422", "500", "503"} <= set(responses)
            for status in ("400", "403", "404", "409", "422", "500", "503"):
                assert (
                    responses[status]["content"]["application/json"]["schema"]["$ref"]
                    == "#/components/schemas/ErrorEnvelope"
                )
    assert len(operation_ids) == len(set(operation_ids))


def test_openapi_matches_approved_snapshot() -> None:
    schema = create_app(RejectingInvoker()).openapi()
    digest = hashlib.sha256(
        json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    expected = (
        (Path(__file__).with_name("snapshots").joinpath("epic009-openapi.sha256"))
        .read_text(encoding="utf-8")
        .strip()
    )
    assert digest == expected


def test_route_delegates_to_registered_tool_and_wraps_safe_failure() -> None:
    invoker = RejectingInvoker()
    client = TestClient(create_app(invoker))
    response = client.post(
        "/api/v1/workspaces",
        json={
            "request_id": "00000000-0000-0000-0000-000000000001",
            "workspace_id": "00000000-0000-0000-0000-000000000002",
            "actor": {"actor_id": "local-user"},
            "input": {
                "root_path": "C:\\approved",
                "read_only": True,
                "allowed_output_subpaths": [],
            },
        },
    )
    assert response.status_code == 400
    assert invoker.calls[0][0] == "TOOL-001"
    assert response.json() == {
        "errors": [
            {
                "code": "TEST_REJECTION",
                "category": "internal",
                "message": "Safe test rejection.",
                "retryable": False,
                "path": None,
                "details": {},
            }
        ]
    }


def test_request_validation_is_always_a_tool_error_envelope() -> None:
    response = TestClient(create_app(RejectingInvoker())).post(
        "/api/v1/workspaces",
        json={"root_path": "../escape"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["errors"][0]["code"] == "API_INPUT_INVALID"
    assert body["errors"][0]["category"] == "validation"
    assert "input" not in str(body)


@pytest.mark.parametrize(
    ("path", "action"),
    [
        ("/api/v1/jobs/events", "cancel"),
        ("/api/v1/jobs/cancel", "query"),
    ],
)
def test_job_action_routes_reject_caller_supplied_cross_action(path: str, action: str) -> None:
    response = TestClient(create_app(RejectingInvoker())).post(
        path,
        json={
            "request_id": "00000000-0000-0000-0000-000000000001",
            "workspace_id": "00000000-0000-0000-0000-000000000002",
            "actor": {"actor_id": "local-user"},
            "input": {
                "action": action,
                "job_id": "00000000-0000-0000-0000-000000000003",
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "API_INPUT_INVALID"


@pytest.mark.parametrize(
    ("path", "tool_id"),
    [
        ("/api/v1/inventories/discover", "TOOL-002"),
        ("/api/v1/inventories/classify", "TOOL-005"),
        ("/api/v1/inventories/reset", "TOOL-022"),
        ("/api/v1/jobs", "TOOL-025"),
        ("/api/v1/jobs/control", "TOOL-026"),
        ("/api/v1/jobs/events", "TOOL-026"),
        ("/api/v1/jobs/cancel", "TOOL-026"),
        ("/api/v1/manifests", "TOOL-014"),
        ("/api/v1/associations/match", "TOOL-016"),
        ("/api/v1/associations", "TOOL-023"),
        ("/api/v1/learning/learn", "TOOL-017"),
        ("/api/v1/learning/models", "TOOL-024"),
        ("/api/v1/generation/one", "TOOL-018"),
        ("/api/v1/validation", "TOOL-020"),
        ("/api/v1/review/inventories", "TOOL-027"),
        ("/api/v1/review/manifests", "TOOL-028"),
        ("/api/v1/review/decisions", "TOOL-029"),
        ("/api/v1/exports", "TOOL-030"),
    ],
)
def test_every_route_delegates_to_its_registered_tool(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    tool_id: str,
) -> None:
    sentinel = SimpleNamespace(input=SimpleNamespace(dry_run=True))
    monkeypatch.setattr("agentic_osdu.api.routes._parse", lambda *_args: sentinel)
    monkeypatch.setattr(
        "agentic_osdu.api.routes._job_control_request", lambda value, _action: value
    )
    invoker = RejectingInvoker()
    response = TestClient(create_app(invoker)).post(path, json={})
    assert response.status_code == 400
    assert invoker.calls == [(tool_id, sentinel)]
