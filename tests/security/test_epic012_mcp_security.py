from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from mcp_types import CallToolResult

from agentic_osdu.agents.orchestrator import OrchestrationError
from agentic_osdu.agents.workflows import WorkflowId, build_workflow_plan
from agentic_osdu.domain.models import ActorRef
from agentic_osdu.mcp_server import create_mcp_server, mcp_tool_name
from agentic_osdu.runtime import create_runtime
from agentic_osdu.schemas import catalog as catalog_module
from agentic_osdu.tools.contracts import (
    TOOL_REGISTRY,
    LocalSchemaCatalogImport,
    RegisterWorkspaceInput,
    SchemaCatalogSource,
    SchemaChecksum,
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


_MALFORMED_PATH_SENTINEL = r"C:\TOP-SECRET\credentials.json"
_MALFORMED_CREDENTIAL_SENTINEL = "SECRET_TOKEN_EPIC_012"
_EXPORT_SENTINEL = "SECRET_EXPORT_EPIC_012"


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


def test_mcp_tool_manager_redacts_malformed_request_envelope(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    invoker = NoInvocationInvoker()

    result = asyncio.run(
        create_mcp_server(invoker).call_tool(
            mcp_tool_name(TOOL_REGISTRY["TOOL-001"]),
            {
                "input": {
                    "root_path": _MALFORMED_PATH_SENTINEL,
                    "api_token": _MALFORMED_CREDENTIAL_SENTINEL,
                }
            },
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["errors"][0]["code"] == "TOOL_INPUT_INVALID"
    serialized = json.dumps(result.model_dump(mode="json"))
    logs = caplog.text
    for sentinel in (_MALFORMED_PATH_SENTINEL, _MALFORMED_CREDENTIAL_SENTINEL):
        assert sentinel not in serialized
        assert sentinel not in logs
    assert invoker.calls == []


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


def test_every_workflow_approval_step_is_rejected_before_direct_mcp_dispatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    approval_gated_tool_ids = {
        step.tool_id
        for workflow_id in WorkflowId
        for step in build_workflow_plan(workflow_id).steps
        if step.requires_approval
    }
    approval_gated_inputs: dict[str, dict[str, object]] = {
        "TOOL-018": {
            "file_id": str(uuid4()),
            "learning_model_id": str(uuid4()),
            "generation_policy_version": "1.0.0",
            "dry_run": False,
        },
        "TOOL-024": {"mutation": {"action": "clear"}},
        "TOOL-025": {
            "definition": {
                "job_type": "approval-audit",
                "steps": [{"sequence": 1, "tool_id": "TOOL-020", "input_ref": "validation"}],
            },
            "deduplication_key": "epic-012-approval-audit",
            "workflow_id": "WF-005",
            "generation": {
                "inventory_id": str(uuid4()),
                "schema_catalog_id": str(uuid4()),
                "generation_policy_version": "1.0.0",
                "dry_run": True,
            },
        },
        "TOOL-029": {
            "decision": {
                "decision_id": str(uuid4()),
                "actor": {"actor_id": "human.operator"},
                "target_type": "generated_candidate",
                "target_id": str(uuid4()),
                "target_version": "1",
                "decision": "approve",
                "reason": "approval audit",
                "decided_at": "2026-09-12T20:00:00Z",
            }
        },
        "TOOL-030": {
            "request": {
                "export_kind": "approved_manifest",
                "target_id": str(uuid4()),
                "output_root_id": "generated",
                "relative_path": f"exports/{_EXPORT_SENTINEL}.json",
                "expected_target_version": _EXPORT_SENTINEL,
            }
        },
    }
    assert approval_gated_tool_ids == set(approval_gated_inputs)

    invoker = NoInvocationInvoker()
    for tool_id in sorted(approval_gated_tool_ids):
        result = _call(tool_id, invoker, approval_gated_inputs[tool_id])
        assert result.is_error is True
        assert result.structured_content is not None
        assert result.structured_content["errors"][0]["code"] == "HUMAN_APPROVAL_REQUIRED"
        assert _EXPORT_SENTINEL not in json.dumps(result.model_dump(mode="json"))
    assert invoker.calls == []
    assert _EXPORT_SENTINEL not in caplog.text


def test_direct_mcp_preserves_non_approval_job_dispatch() -> None:
    invoker = NoInvocationInvoker()

    result = _call(
        "TOOL-025",
        invoker,
        {
            "definition": {
                "job_type": "generic-audit",
                "steps": [{"sequence": 1, "tool_id": "TOOL-020", "input_ref": "validation"}],
            },
            "deduplication_key": "epic-012-generic-job",
        },
    )

    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["errors"][0]["code"] == "TOOL_EXECUTION_FAILED"
    assert invoker.calls == ["TOOL-025"]


def test_direct_mcp_preserves_ungated_generation_dry_run_dispatch() -> None:
    invoker = NoInvocationInvoker()

    result = _call(
        "TOOL-018",
        invoker,
        {
            "file_id": str(uuid4()),
            "learning_model_id": str(uuid4()),
            "generation_policy_version": "1.0.0",
            "dry_run": True,
        },
    )

    assert result.is_error is True
    assert result.structured_content is not None
    assert result.structured_content["errors"][0]["code"] == "TOOL_EXECUTION_FAILED"
    assert invoker.calls == ["TOOL-018"]


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


def test_mcp_schema_import_rejects_root_identity_swap_before_consuming_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    approved_catalog = workspace_root / "catalog"
    relative = Path("manifest") / "Manifest.1.0.0.json"
    payload = b'{"schema":{"type":"object","title":"external"}}'
    target = approved_catalog / relative
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

        original_open = catalog_module._open_bound_descriptor
        identity_checks = 0

        def swapped_identity(
            path: Path,
            *,
            require_directory: bool = False,
            require_file: bool = False,
            error_code: str = "ROOT_POLICY_DENIED",
        ) -> Any:
            nonlocal identity_checks
            descriptor, identity, physical_path = original_open(
                path,
                require_directory=require_directory,
                require_file=require_file,
                error_code=error_code,
            )
            if path == approved_catalog:
                identity_checks += 1
                if identity_checks > 1:
                    identity = replace(identity, inode=identity.inode + 1)
            return descriptor, identity, physical_path

        external_bytes_consumed = False
        original_read = catalog_module._read_bound_file

        def record_read(*args: Any, **kwargs: Any) -> bytes:
            nonlocal external_bytes_consumed
            external_bytes_consumed = True
            return original_read(*args, **kwargs)

        monkeypatch.setattr(catalog_module, "_open_bound_descriptor", swapped_identity)
        monkeypatch.setattr(catalog_module, "_read_bound_file", record_read)

        denied = _call(
            "TOOL-021",
            runtime.registry,
            {
                "source": "local_export",
                "revision": "epic-012-race",
                "local_root": str(approved_catalog),
                "expected_checksums": [
                    {
                        "relative_path": relative.as_posix(),
                        "sha256": sha256(payload).hexdigest(),
                    }
                ],
            },
            workspace_id=workspace_id,
        )

        assert denied.is_error is True
        assert denied.structured_content is not None
        assert denied.structured_content["errors"][0]["code"] == "ROOT_POLICY_DENIED"
        assert external_bytes_consumed is False
    finally:
        runtime.database.dispose()


def test_mcp_schema_import_rejects_workspace_replaced_after_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
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

        approved_root = tmp_path / "approved-root"
        workspace_root.rename(approved_root)
        relative = Path("catalog") / "manifest" / "Manifest.1.0.0.json"
        external_payload = b'{"schema":{"type":"object","title":"outside-approved-root"}}'
        replacement_target = workspace_root / relative
        replacement_target.parent.mkdir(parents=True)
        replacement_target.write_bytes(external_payload)

        external_bytes_consumed = False
        original_read = os.read

        def record_external_read(descriptor: int, size: int) -> bytes:
            nonlocal external_bytes_consumed
            if catalog_module._physical_path_from_descriptor(
                descriptor,
                replacement_target,
            ) == catalog_module._normalize_physical_path(str(replacement_target)):
                external_bytes_consumed = True
            return original_read(descriptor, size)

        monkeypatch.setattr(os, "read", record_external_read)
        denied = _call(
            "TOOL-021",
            runtime.registry,
            {
                "source": "local_export",
                "revision": "epic-012-approved-root-swap",
                "local_root": str(workspace_root / "catalog"),
                "expected_checksums": [
                    {
                        "relative_path": "manifest/Manifest.1.0.0.json",
                        "sha256": sha256(external_payload).hexdigest(),
                    }
                ],
            },
            workspace_id=registered.output.workspace_id,
        )

        assert denied.is_error is True
        assert denied.structured_content is not None
        assert denied.structured_content["errors"][0]["code"] == "ROOT_POLICY_DENIED"
        assert str(approved_root) not in str(denied.structured_content)
        assert str(workspace_root) not in str(denied.structured_content)
        assert external_bytes_consumed is False
    finally:
        runtime.database.dispose()


def test_bound_schema_read_rejects_outside_descriptor_before_consuming_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved_root = tmp_path / "approved"
    approved_target = approved_root / "manifest" / "Manifest.1.0.0.json"
    approved_target.parent.mkdir(parents=True)
    approved_target.write_bytes(b'{"schema":{"type":"object"}}')
    outside_target = tmp_path / "outside.json"
    outside_target.write_bytes(b'{"schema":{"title":"outside-approved-root"}}')
    capability = catalog_module.LocalSchemaReadCapability.capture(approved_root)
    original_open = catalog_module._open_bound_descriptor
    outside_descriptor: int | None = None
    external_bytes_consumed = False

    def redirect_file_open(
        path: Path,
        *,
        require_directory: bool = False,
        require_file: bool = False,
        error_code: str = "ROOT_POLICY_DENIED",
    ) -> Any:
        nonlocal outside_descriptor
        if path == approved_target:
            descriptor, identity, physical_path = original_open(
                outside_target,
                require_file=True,
                error_code=error_code,
            )
            outside_descriptor = descriptor
            return descriptor, identity, physical_path
        return original_open(
            path,
            require_directory=require_directory,
            require_file=require_file,
            error_code=error_code,
        )

    original_read = os.read

    def record_external_read(descriptor: int, size: int) -> bytes:
        nonlocal external_bytes_consumed
        if descriptor == outside_descriptor:
            external_bytes_consumed = True
        return original_read(descriptor, size)

    monkeypatch.setattr(catalog_module, "_open_bound_descriptor", redirect_file_open)
    monkeypatch.setattr(os, "read", record_external_read)
    try:
        with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
            catalog_module._read_bound_file(
                capability,
                "manifest/Manifest.1.0.0.json",
            )
        assert external_bytes_consumed is False
    finally:
        capability.close()


def test_mcp_schema_import_does_not_activate_when_source_changes_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    approved_catalog = workspace_root / "catalog"
    relative = Path("manifest") / "Manifest.1.0.0.json"
    payload = b'{"schema":{"type":"object"}}'
    target = approved_catalog / relative
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
        original_read = catalog_module._read_bound_file

        def replace_after_read(
            capability: catalog_module.LocalSchemaReadCapability,
            relative_path: str,
        ) -> bytes:
            consumed = original_read(capability, relative_path)
            observed_path = capability.root / Path(relative_path)
            identity = capability._observed_files[observed_path]
            capability._observed_files[observed_path] = replace(
                identity,
                inode=identity.inode + 1,
            )
            return consumed

        monkeypatch.setattr(catalog_module, "_read_bound_file", replace_after_read)
        denied = _call(
            "TOOL-021",
            runtime.registry,
            {
                "source": "local_export",
                "revision": "epic-012-post-read-race",
                "local_root": str(approved_catalog),
                "expected_checksums": [
                    {
                        "relative_path": relative.as_posix(),
                        "sha256": sha256(payload).hexdigest(),
                    }
                ],
            },
            workspace_id=registered.output.workspace_id,
        )

        assert denied.is_error is True
        assert denied.structured_content is not None
        assert denied.structured_content["errors"][0]["code"] == "ROOT_POLICY_DENIED"
        with pytest.raises(catalog_module.SchemaCatalogError, match="SCHEMA_UNAVAILABLE"):
            runtime.schema_catalog_store.active_catalog()
    finally:
        runtime.database.dispose()


def test_local_schema_capability_rejects_root_and_observation_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved_root = tmp_path / "approved"
    other_root = tmp_path / "other"
    observed_directory = approved_root / "manifest"
    observed_file = observed_directory / "Manifest.1.0.0.json"
    observed_directory.mkdir(parents=True)
    other_root.mkdir()
    observed_file.write_bytes(b'{"schema":{"type":"object"}}')
    capability = catalog_module.LocalSchemaReadCapability.capture(approved_root)

    request = LocalSchemaCatalogImport(
        source=SchemaCatalogSource.LOCAL_EXPORT,
        revision="epic-012-capability-mismatch",
        local_root=str(other_root),
        expected_checksums=(
            SchemaChecksum(
                relative_path="manifest/Manifest.1.0.0.json",
                sha256="0" * 64,
            ),
        ),
    )
    with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
        catalog_module.SchemaCatalogStore(tmp_path / "cache").import_local(
            request,
            capability=capability,
        )

    capability = catalog_module.LocalSchemaReadCapability.capture(approved_root)
    capability.observe_directory(observed_directory)
    held_after_directory = len(capability._descriptors)
    capability.observe_directory(observed_directory)
    assert len(capability._descriptors) == held_after_directory
    descriptor, file_identity, _ = catalog_module._open_bound_descriptor(
        observed_file,
        require_file=True,
    )
    try:
        capability.observe_file(observed_file, file_identity, descriptor)
        held_after_file = len(capability._descriptors)
        capability.observe_file(observed_file, file_identity, descriptor)
        assert len(capability._descriptors) == held_after_file
        with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
            capability.observe_file(
                observed_file,
                replace(file_identity, inode=file_identity.inode + 1),
                descriptor,
            )
    finally:
        os.close(descriptor)

    original_open = catalog_module._open_bound_descriptor

    def changed_directory_identity(
        path: Path,
        *,
        require_directory: bool = False,
        require_file: bool = False,
        error_code: str = "ROOT_POLICY_DENIED",
    ) -> Any:
        descriptor, identity, physical_path = original_open(
            path,
            require_directory=require_directory,
            require_file=require_file,
            error_code=error_code,
        )
        if path == observed_directory:
            identity = replace(identity, inode=identity.inode + 1)
        return descriptor, identity, physical_path

    monkeypatch.setattr(catalog_module, "_open_bound_descriptor", changed_directory_identity)
    try:
        with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
            capability.observe_directory(observed_directory)
    finally:
        capability.close()


def test_bound_schema_paths_fail_closed_for_invalid_filesystem_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "catalog"
    file_path = directory / "schema.json"
    directory.mkdir()
    file_path.write_text("{}", encoding="utf-8")

    with pytest.raises(catalog_module.SchemaCatalogError, match="CATALOG_INCOMPLETE"):
        catalog_module._open_bound_descriptor(
            tmp_path / "missing",
            require_file=True,
            error_code="CATALOG_INCOMPLETE",
        )
    with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
        catalog_module._open_bound_descriptor(file_path, require_directory=True)
    with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
        catalog_module._open_bound_descriptor(directory, require_file=True)

    monkeypatch.setattr(catalog_module, "_is_link_or_reparse", lambda _: True)
    with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
        catalog_module._open_bound_descriptor(file_path, require_file=True)


def test_bound_schema_paths_require_original_physical_root_and_identity(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
        catalog_module._require_physical_containment(root, outside)

    descriptor, identity, physical_root = catalog_module._open_bound_descriptor(
        root,
        require_directory=True,
    )
    os.close(descriptor)
    with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
        catalog_module._require_same_identity_and_location(
            root,
            replace(identity, inode=identity.inode + 1),
            physical_root,
            require_directory=True,
        )
    with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
        catalog_module._require_same_identity_and_exact_location(
            root,
            identity,
            outside,
            require_directory=True,
        )

    assert catalog_module._normalize_physical_path(r"\\?\C:\catalog") == Path(r"c:\catalog")
    assert catalog_module._normalize_physical_path(r"\\?\UNC\server\catalog") == Path(
        r"\\server\catalog"
    )


def test_local_schema_capability_rejects_intermediate_path_redirection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    catalog = workspace / "nested" / "catalog"
    outside = tmp_path / "outside"
    catalog.mkdir(parents=True)
    outside.mkdir()
    anchor = catalog_module.LocalSchemaReadCapability.capture(workspace)
    original_open = catalog_module._open_bound_descriptor

    def redirected_component(
        path: Path,
        *,
        require_directory: bool = False,
        require_file: bool = False,
        error_code: str = "ROOT_POLICY_DENIED",
    ) -> Any:
        descriptor, identity, physical_path = original_open(
            path,
            require_directory=require_directory,
            require_file=require_file,
            error_code=error_code,
        )
        if path == workspace / "nested":
            physical_path = outside
        return descriptor, identity, physical_path

    monkeypatch.setattr(catalog_module, "_open_bound_descriptor", redirected_component)
    try:
        with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
            catalog_module.LocalSchemaReadCapability.capture(
                catalog,
                error_code="ROOT_POLICY_DENIED",
                anchor=anchor,
            )
    finally:
        anchor.close()


def test_local_schema_capability_handles_same_root_anchor_and_outside_rejection(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()

    anchor = catalog_module.LocalSchemaReadCapability.capture(workspace)
    with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
        catalog_module.LocalSchemaReadCapability.capture(
            outside,
            error_code="ROOT_POLICY_DENIED",
            anchor=anchor,
        )

    same_root = catalog_module.LocalSchemaReadCapability.capture(
        workspace,
        error_code="ROOT_POLICY_DENIED",
        anchor=anchor,
    )
    assert same_root.identity_tuple == anchor.identity_tuple
    same_root.close()
    assert same_root._descriptors == []
    assert same_root._anchor is None

    drive_root = catalog_module.LocalSchemaReadCapability.capture(Path(tmp_path.anchor))
    assert drive_root.root == Path(tmp_path.anchor)
    drive_root.close()


def test_workspace_capability_rejects_unanchored_ancestor_redirection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    original_open = catalog_module._open_bound_descriptor

    def redirected_ancestor(
        path: Path,
        *,
        require_directory: bool = False,
        require_file: bool = False,
        error_code: str = "ROOT_POLICY_DENIED",
    ) -> Any:
        descriptor, identity, physical_path = original_open(
            path,
            require_directory=require_directory,
            require_file=require_file,
            error_code=error_code,
        )
        if path == workspace.parent:
            physical_path = outside
        return descriptor, identity, physical_path

    monkeypatch.setattr(catalog_module, "_open_bound_descriptor", redirected_ancestor)
    with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
        catalog_module.LocalSchemaReadCapability.capture(
            workspace,
            error_code="ROOT_POLICY_DENIED",
        )


def test_bound_descriptor_maps_windows_reparse_open_failure_to_policy_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reparse_open_failure(path: Path, *, require_directory: bool) -> int:
        del path, require_directory
        error = OSError(22, "reparse open denied")
        error.winerror = 1920
        raise error

    monkeypatch.setattr(catalog_module, "_open_windows_descriptor", reparse_open_failure)
    with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
        catalog_module._open_bound_descriptor(tmp_path / "reparse", require_directory=True)


def test_mcp_schema_import_requires_an_approved_persisted_workspace(tmp_path: Path) -> None:
    runtime = create_runtime(tmp_path / "state.db")
    try:
        denied = _call(
            "TOOL-021",
            runtime.registry,
            {
                "source": "local_export",
                "revision": "epic-012-missing-workspace",
                "local_root": str(tmp_path / "catalog"),
                "expected_checksums": [
                    {
                        "relative_path": "manifest/Manifest.1.0.0.json",
                        "sha256": "0" * 64,
                    }
                ],
            },
        )
        assert denied.is_error is True
        assert denied.structured_content is not None
        assert denied.structured_content["errors"][0]["code"] == "ROOT_POLICY_DENIED"
        assert str(tmp_path) not in str(denied.structured_content)
    finally:
        runtime.database.dispose()


def test_bound_schema_read_rejects_oversized_local_schema(tmp_path: Path) -> None:
    approved_root = tmp_path / "approved"
    target = approved_root / "schema.json"
    approved_root.mkdir()
    target.write_bytes(b"x" * (catalog_module._MAX_SCHEMA_BYTES + 1))
    capability = catalog_module.LocalSchemaReadCapability.capture(approved_root)
    try:
        with pytest.raises(catalog_module.SchemaCatalogError, match="CATALOG_INCOMPLETE"):
            catalog_module._read_bound_file(capability, "schema.json")
    finally:
        capability.close()


def test_bound_schema_read_rejects_file_identity_change_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved_root = tmp_path / "approved"
    target = approved_root / "schema.json"
    approved_root.mkdir()
    target.write_text("{}", encoding="utf-8")
    capability = catalog_module.LocalSchemaReadCapability.capture(approved_root)
    original_identity = catalog_module._identity_from_metadata
    regular_file_checks = 0

    def changed_file_identity(metadata: os.stat_result) -> Any:
        nonlocal regular_file_checks
        identity = original_identity(metadata)
        if stat.S_ISREG(metadata.st_mode):
            regular_file_checks += 1
            if regular_file_checks > 1:
                return replace(identity, inode=identity.inode + 1)
        return identity

    monkeypatch.setattr(catalog_module.LocalSchemaReadCapability, "validate", lambda self: None)
    monkeypatch.setattr(catalog_module, "_identity_from_metadata", changed_file_identity)
    try:
        with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
            catalog_module._read_bound_file(capability, "schema.json")
    finally:
        capability.close()


def test_pre_cancelled_local_import_closes_supplied_capability(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    capability = catalog_module.LocalSchemaReadCapability.capture(source)
    request = LocalSchemaCatalogImport(
        source=SchemaCatalogSource.LOCAL_EXPORT,
        revision="epic-012-pre-cancelled",
        local_root=str(source),
        expected_checksums=(
            SchemaChecksum(
                relative_path="manifest/Manifest.1.0.0.json",
                sha256="0" * 64,
            ),
        ),
    )

    with pytest.raises(catalog_module.SchemaCatalogError, match="CANCELLED"):
        catalog_module.SchemaCatalogStore(tmp_path / "cache").import_local(
            request,
            cancellation=lambda: True,
            capability=capability,
        )

    assert capability._descriptors == []


def test_local_schema_install_rolls_back_catalog_if_post_publish_validation_fails(
    tmp_path: Path,
) -> None:
    payload = b'{"schema":{"type":"object"}}'
    checksum = SchemaChecksum(
        relative_path="manifest/Manifest.1.0.0.json",
        sha256=sha256(payload).hexdigest(),
    )
    validation_count = 0

    def validate_source() -> None:
        nonlocal validation_count
        validation_count += 1
        if validation_count == 2:
            raise catalog_module.SchemaCatalogError(
                "ROOT_POLICY_DENIED",
                "The approved local schema path changed during import.",
            )

    store = catalog_module.SchemaCatalogStore(tmp_path / "cache")
    with pytest.raises(catalog_module.SchemaCatalogError, match="ROOT_POLICY_DENIED"):
        store._install(
            revision="epic-012-publish-race",
            source=f"local_export:{tmp_path / 'source'}",
            expected=(checksum,),
            reader=lambda _: payload,
            cancellation=None,
            before_publish=validate_source,
        )

    assert store.list_catalogs() == ()
    with pytest.raises(catalog_module.SchemaCatalogError, match="SCHEMA_UNAVAILABLE"):
        store.active_catalog()


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
