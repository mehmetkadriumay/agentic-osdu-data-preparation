from __future__ import annotations

import json

from agentic_osdu.cli import main


def test_cli_primary_workflows_emit_json_and_use_expected_routes(capsys: object) -> None:
    calls: list[str] = []

    def transport(route: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        calls.append(route)
        return 200, {"status": "succeeded", "route": route, "request": payload}

    commands = {
        "discover": "/api/v1/inventories/discover",
        "classify": "/api/v1/inventories/classify",
        "manifest-match": "/api/v1/associations/match",
        "manifest-learn": "/api/v1/learning/learn",
        "manifest-generate": "/api/v1/generation/one",
        "manifest-generate-all": "/api/v1/generation/all",
        "manifest-validate": "/api/v1/validation",
        "job-status": "/api/v1/jobs/events",
        "job-cancel": "/api/v1/jobs/cancel",
        "review-export": "/api/v1/exports",
    }
    for command, route in commands.items():
        assert main([command, "--json", "{}"], transport=transport) == 0
        assert calls[-1] == route
        output = capsys.readouterr().out  # type: ignore[attr-defined]
        assert json.loads(output)["status"] == "succeeded"


def test_cli_returns_stable_nonzero_exit_for_api_error(capsys: object) -> None:
    def rejected(_route: str, _payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        return 409, {
            "errors": [
                {
                    "code": "STATE_VERSION_CONFLICT",
                    "category": "conflict",
                    "message": "State changed.",
                    "retryable": True,
                    "path": None,
                    "details": {},
                }
            ]
        }

    assert main(["discover", "--json", "{}"], transport=rejected) == 3
    assert json.loads(capsys.readouterr().out)["errors"][0]["code"] == "STATE_VERSION_CONFLICT"  # type: ignore[attr-defined]


def test_cli_returns_nonzero_for_failed_tool_result(capsys: object) -> None:
    def failed(_route: str, _payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        return 200, {"status": "failed", "errors": [{"code": "TOOL_FAILED"}]}

    assert main(["classify", "--json", "{}"], transport=failed) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "failed"  # type: ignore[attr-defined]


def test_cli_has_no_migration_command() -> None:
    assert main(["migration", "--json", "{}"], transport=_unused_transport) == 2


def test_cli_rejects_unsafe_api_url_with_json_exit_code(capsys: object) -> None:
    assert main(["--api-url", "file:///state.db", "discover", "--json", "{}"]) == 4
    assert json.loads(capsys.readouterr().out)["errors"][0]["code"] == "API_UNAVAILABLE"  # type: ignore[attr-defined]


def _unused_transport(_route: str, _payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    raise AssertionError("transport must not be called")
