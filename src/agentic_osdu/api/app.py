"""FastAPI composition boundary for typed deterministic-tool routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from agentic_osdu.agents.orchestrator import OrchestrationError
from agentic_osdu.api.routes import RegisteredToolInvoker, router
from agentic_osdu.domain.models import ToolError, ToolErrorCategory
from agentic_osdu.jobs.service import JobError
from agentic_osdu.tools.review import ReviewError


class ErrorEnvelope(BaseModel):
    errors: tuple[ToolError, ...]


def bundled_web_directory() -> Path:
    """Return UI assets shipped inside the Python package."""
    return Path(__file__).resolve().parents[1] / "web"


def _error(
    code: str,
    category: ToolErrorCategory,
    message: str,
    *,
    retryable: bool = False,
) -> ErrorEnvelope:
    return ErrorEnvelope(
        errors=(
            ToolError(
                code=code,
                category=category,
                message=message,
                retryable=retryable,
            ),
        )
    )


def create_app(
    tool_invoker: RegisteredToolInvoker,
    *,
    web_directory: Path | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Agentic OSDU Data Preparation API",
        version="1.0.0",
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
    )
    app.state.tool_invoker = tool_invoker

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        if error.status_code == 404:
            envelope = _error(
                "API_ROUTE_NOT_FOUND",
                ToolErrorCategory.NOT_FOUND,
                "The requested API route was not found.",
            )
        elif error.status_code == 405:
            envelope = _error(
                "API_METHOD_NOT_ALLOWED",
                ToolErrorCategory.VALIDATION,
                "The HTTP method is not allowed for this API route.",
            )
        else:
            envelope = _error(
                "API_HTTP_ERROR",
                ToolErrorCategory.VALIDATION,
                "The HTTP request could not be completed.",
            )
        return JSONResponse(status_code=error.status_code, content=envelope.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        _request: Request, _error_value: RequestValidationError
    ) -> JSONResponse:
        envelope = _error(
            "API_INPUT_INVALID",
            ToolErrorCategory.VALIDATION,
            "The request did not match the published API contract.",
        )
        return JSONResponse(status_code=422, content=envelope.model_dump(mode="json"))

    @app.exception_handler(OrchestrationError)
    @app.exception_handler(JobError)
    @app.exception_handler(ReviewError)
    @app.exception_handler(RuntimeError)
    async def tool_boundary_error(request: Request, error: Exception) -> JSONResponse:
        del request
        code = getattr(error, "code", "TOOL_EXECUTION_FAILED")
        category = _category_for_code(code)
        message = str(error).partition(": ")[2] or "The operation could not be completed."
        envelope = _error(
            code,
            category,
            message,
            retryable=bool(getattr(error, "retryable", False)),
        )
        status_code = (
            404
            if category is ToolErrorCategory.NOT_FOUND
            else 403
            if category is ToolErrorCategory.ACCESS_DENIED
            else 409
            if category in {ToolErrorCategory.CONFLICT, ToolErrorCategory.CANCELLED}
            else 503
            if category is ToolErrorCategory.SCHEMA_UNAVAILABLE
            else 500
            if category is ToolErrorCategory.IO or code == "TOOL_EXECUTION_FAILED"
            else 400
        )
        return JSONResponse(status_code=status_code, content=envelope.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def unexpected_tool_error(request: Request, error: Exception) -> JSONResponse:
        if hasattr(error, "code"):
            return await tool_boundary_error(request, error)
        del request
        envelope = _error(
            "TOOL_EXECUTION_FAILED",
            ToolErrorCategory.INTERNAL,
            "The operation failed inside the configured tool boundary.",
        )
        return JSONResponse(status_code=500, content=envelope.model_dump(mode="json"))

    app.include_router(
        router,
        responses={
            400: {"model": ErrorEnvelope},
            403: {"model": ErrorEnvelope},
            404: {"model": ErrorEnvelope},
            409: {"model": ErrorEnvelope},
            422: {"model": ErrorEnvelope},
            500: {"model": ErrorEnvelope},
            503: {"model": ErrorEnvelope},
        },
    )
    if web_directory is not None:
        app.mount(
            "/",
            StaticFiles(directory=web_directory, html=True, check_dir=False),
            name="review-ui",
        )
    return app


def _category_for_code(code: str) -> ToolErrorCategory:
    if code == "CANCELLED":
        return ToolErrorCategory.CANCELLED
    if code.endswith("_NOT_FOUND"):
        return ToolErrorCategory.NOT_FOUND
    if "DENIED" in code or code in {"HUMAN_APPROVAL_REQUIRED", "NETWORK_NOT_APPROVED"}:
        return ToolErrorCategory.ACCESS_DENIED
    if code in {
        "SCHEMA_UNAVAILABLE",
        "SCHEMA_REFERENCE_FAILED",
        "CATALOG_INCOMPLETE",
        "CHECKSUM_MISMATCH",
    }:
        return ToolErrorCategory.SCHEMA_UNAVAILABLE
    if (
        "CONFLICT" in code
        or "ALREADY" in code
        or code
        in {
            "MODEL_IN_USE",
            "OUTPUT_EXISTS",
            "NO_COMPATIBLE_MODEL",
            "STALE_REVIEW_TARGET",
            "JOB_NOT_CANCELLABLE",
        }
    ):
        return ToolErrorCategory.CONFLICT
    if code.startswith(("IO_", "DB_")):
        return ToolErrorCategory.IO
    if code.startswith("UNSUPPORTED_"):
        return ToolErrorCategory.UNSUPPORTED
    if code in {
        "CLASSIFICATION_INCONCLUSIVE",
        "NO_ELIGIBLE_EXAMPLES",
        "GENERATED_EXAMPLE_REJECTED",
        "BATCH_POLICY_INVALID",
        "JOB_DEFINITION_INVALID",
        "MATCH_INPUT_INCOMPLETE",
        "TOOL_INPUT_INVALID",
        "VALIDATION_FAILED",
    }:
        return ToolErrorCategory.VALIDATION
    if code.endswith(("_INVALID", "_FAILED", "_TRUNCATED", "_AMBIGUOUS")):
        return ToolErrorCategory.PARSE
    if code in {"INSUFFICIENT_SAMPLE", "FILE_LIMIT_EXCEEDED", "READ_LIMIT_EXCEEDED"}:
        return ToolErrorCategory.VALIDATION
    return ToolErrorCategory.INTERNAL


__all__ = ["ErrorEnvelope", "bundled_web_directory", "create_app"]
