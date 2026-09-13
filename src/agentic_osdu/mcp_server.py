"""Native MCP transport adapter for the registered deterministic tool runtime."""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, Protocol

from mcp.server import MCPServer
from mcp.server.mcpserver.tools import Tool
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import TypeAdapter, ValidationError

from agentic_osdu.api.app import ErrorEnvelope, _category_for_code
from agentic_osdu.domain.models import ToolError, ToolErrorCategory
from agentic_osdu.runtime import create_runtime
from agentic_osdu.tools.contracts import (
    TOOL_REGISTRY,
    AccessMode,
    ApprovedRemoteSchemaCatalogRefresh,
    ToolDefinition,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
)

_SERVER_NAME = "agentic-osdu-data-preparation"
_SERVER_VERSION = "1.0.0"


class RegisteredToolInvoker(Protocol):
    def invoke(self, tool_id: str, request: ToolRequest[Any]) -> ToolResult[Any]: ...


def mcp_tool_name(definition: ToolDefinition) -> str:
    """Return a stable, readable MCP name tied to the registry ID."""

    slug = re.sub(r"[^a-z0-9]+", "_", definition.name.casefold()).strip("_")
    return f"{definition.tool_id.casefold().replace('-', '_')}_{slug}"


def _safe_error(error: Exception) -> ErrorEnvelope:
    code = (
        "TOOL_INPUT_INVALID"
        if isinstance(error, ValidationError)
        else getattr(error, "code", "TOOL_EXECUTION_FAILED")
    )
    category = _category_for_code(code)
    if category is ToolErrorCategory.ACCESS_DENIED:
        message = "The operation was denied by the configured policy."
    elif category is ToolErrorCategory.CANCELLED:
        message = "The operation was cancelled at a cooperative checkpoint."
    elif code == "TOOL_EXECUTION_FAILED":
        message = "The operation failed inside the configured tool boundary."
    else:
        message = "The operation could not be completed."
    return ErrorEnvelope(
        errors=(
            ToolError(
                code=code,
                category=category,
                message=message,
                retryable=bool(getattr(error, "retryable", False)),
            ),
        )
    )


def _reject_unapproved_direct_action(tool_id: str, request: ToolRequest[Any]) -> None:
    if tool_id in {"TOOL-018", "TOOL-019"} and not request.input.dry_run:
        from agentic_osdu.agents.orchestrator import OrchestrationError

        raise OrchestrationError(
            "HUMAN_APPROVAL_REQUIRED",
            "Write-mode generation must execute through the signed approval boundary.",
        )
    if tool_id == "TOOL-021" and isinstance(request.input.root, ApprovedRemoteSchemaCatalogRefresh):
        from agentic_osdu.agents.orchestrator import OrchestrationError

        raise OrchestrationError(
            "NETWORK_NOT_APPROVED",
            "Remote schema refresh requires a trusted network approval.",
        )
    if tool_id == "TOOL-024":
        from agentic_osdu.agents.orchestrator import OrchestrationError

        raise OrchestrationError(
            "HUMAN_APPROVAL_REQUIRED",
            "Learning-model mutations must execute through the signed WF-003 approval boundary.",
        )
    if tool_id == "TOOL-025" and (
        request.input.generation is not None and not request.input.generation.dry_run
    ):
        from agentic_osdu.agents.orchestrator import OrchestrationError

        raise OrchestrationError(
            "HUMAN_APPROVAL_REQUIRED",
            "Write-mode generation jobs require a signed human approval.",
        )
    if tool_id == "TOOL-029":
        from agentic_osdu.agents.orchestrator import OrchestrationError

        raise OrchestrationError(
            "HUMAN_APPROVAL_REQUIRED",
            "Human review decisions cannot be asserted by an MCP client.",
        )


def _handler(
    invoker: RegisteredToolInvoker,
    definition: ToolDefinition,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    request_type = ToolRequest[definition.input_model]  # type: ignore[name-defined]
    result_type = ToolResult[definition.output_model]  # type: ignore[name-defined]
    response_type = result_type | ErrorEnvelope
    request_schema = request_type.model_json_schema()

    def invoke(**values: Any) -> CallToolResult:
        try:
            request = request_type.model_validate_json(json.dumps(values))
            _reject_unapproved_direct_action(definition.tool_id, request)
            result = invoker.invoke(definition.tool_id, request)
            structured = result.model_dump(mode="json")
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(structured, sort_keys=True))],
                structured_content=structured,
                is_error=result.status
                in {
                    ToolResultStatus.FAILED,
                    ToolResultStatus.CANCELLED,
                },
            )
        except Exception as error:
            envelope = _safe_error(error)
            structured = envelope.model_dump(mode="json")
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(structured, sort_keys=True))],
                structured_content=structured,
                is_error=True,
            )

    invoke.__name__ = mcp_tool_name(definition)
    invoke.__doc__ = definition.purpose
    # Keep SDK argument parsing permissive; the published schema below remains exact,
    # while ToolRequest validation and redaction stay inside invoke().
    invoke.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters=(
            inspect.Parameter(
                "request_id",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Any,
                default=None,
            ),
            inspect.Parameter(
                "workspace_id",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Any,
                default=None,
            ),
            inspect.Parameter(
                "actor",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Any,
                default=None,
            ),
            inspect.Parameter(
                "input",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Any,
                default=None,
            ),
            inspect.Parameter(
                "cancellation_token_id",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Any,
                default=None,
            ),
            inspect.Parameter(
                "expected_state_version",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=Any,
                default=None,
            ),
        ),
        return_annotation=CallToolResult,
    )
    output_schema = TypeAdapter(response_type).json_schema()
    output_schema["type"] = "object"
    return invoke, request_schema, output_schema


def _annotations(definition: ToolDefinition) -> ToolAnnotations:
    read_only = definition.access_mode is AccessMode.READ_ONLY
    destructive = definition.access_mode is not AccessMode.READ_ONLY
    return ToolAnnotations(
        title=f"{definition.tool_id}: {definition.name}",
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=True,
        open_world_hint=False,
    )


def create_mcp_server(invoker: RegisteredToolInvoker) -> MCPServer[None]:
    """Create the MCP server by projecting the authoritative typed registry."""

    tools: list[Tool] = []
    for definition in TOOL_REGISTRY.values():
        handler, request_schema, output_schema = _handler(invoker, definition)
        tool = Tool.from_function(
            handler,
            name=mcp_tool_name(definition),
            title=f"{definition.tool_id}: {definition.name}",
            description=(
                f"{definition.purpose} Access={definition.access_mode.value}; "
                f"network={definition.network_access.value}; "
                f"review_required={str(definition.review_required_output).lower()}."
            ),
            annotations=_annotations(definition),
        )
        tool.parameters = request_schema
        tool.fn_metadata.output_schema = output_schema
        tools.append(tool)
    server: MCPServer[None] = MCPServer(
        name=_SERVER_NAME,
        title="Agentic OSDU Data Preparation",
        description=(
            "Local-first deterministic OSDU data preparation tools. Source roots remain "
            "read-only, generated manifests require review, and direct platform loading is "
            "unavailable."
        ),
        version=_SERVER_VERSION,
        tools=tools,
    )
    return server


def entrypoint() -> None:
    """Run the production MCP server over stdio for local MCP hosts."""

    runtime = create_runtime()
    try:
        create_mcp_server(runtime.registry).run("stdio")
    finally:
        runtime.database.dispose()


if __name__ == "__main__":
    entrypoint()


__all__ = ["create_mcp_server", "entrypoint", "mcp_tool_name"]
