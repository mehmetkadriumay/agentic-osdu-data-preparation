"""Cloud-only API; never expose the unauthenticated local API or MCP mount."""

import asyncio
import os
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from agentic_osdu.runtime import create_runtime

from .auth import AccessDenied, EntraAuthenticator
from .gateway import ToolGateway
from .ledger import SqlLedger
from .settings import Settings
from .workflow import RunRequest, Workflow, WorkflowError, receipt


def create_app() -> FastAPI:
    settings = Settings.from_env()
    if not settings.source_root.is_dir() or settings.source_root.is_symlink():
        raise ValueError("The configured source mount is unavailable")
    if os.name != "posix" or not os.statvfs(settings.source_root).f_flag & os.ST_RDONLY:
        raise ValueError("The cloud source volume must be mounted read-only")
    runtime = create_runtime(
        database_url=settings.database_url,
        schema_catalog_path=settings.schema_catalog_root,
    )
    ledger = SqlLedger(runtime.database)
    workflow = Workflow(
        ToolGateway(runtime), ledger, str(settings.source_root), settings.scan_subpath
    )
    auth = EntraAuthenticator(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            runtime.database.dispose()

    app = FastAPI(
        title="OSDU staged exploration backend",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        if request.url.hostname not in settings.allowed_hosts:
            return JSONResponse({"code": "HOST_DENIED"}, status_code=400)
        try:
            request.state.caller = await asyncio.to_thread(
                auth.authenticate, request.headers.get("authorization", "")
            )
        except AccessDenied:
            return JSONResponse(
                {"code": "ACCESS_DENIED"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if request.method == "GET" and not request.state.caller.reader:
            return JSONResponse({"code": "READER_ACCESS_REQUIRED"}, status_code=403)
        if request.method == "POST":
            try:
                length = int(request.headers["content-length"])
            except (KeyError, ValueError):
                return JSONResponse({"code": "CONTENT_LENGTH_REQUIRED"}, status_code=411)
            if not 0 <= length <= 4096:
                return JSONResponse({"code": "REQUEST_TOO_LARGE"}, status_code=413)
        try:
            return await call_next(request)
        except Exception:
            return JSONResponse({"code": "BACKEND_OPERATION_FAILED"}, status_code=500)

    @app.exception_handler(WorkflowError)
    async def workflow_error(_request: Request, error: WorkflowError):
        status = 422 if error.code == "RUN_REQUEST_INVALID" else 409
        if error.code == "RUN_NOT_FOUND":
            status = 404
        return JSONResponse({"code": error.code}, status_code=status)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    def get_run(run_id: UUID) -> dict:
        state = ledger.get(str(run_id))
        if state is None:
            raise WorkflowError("RUN_NOT_FOUND")
        return state

    @app.post("/stages/{stage}")
    def execute_stage(stage: int, value: dict, request: Request):
        caller = request.state.caller
        if caller.stage != stage:
            return JSONResponse({"code": "STAGE_CAPABILITY_DENIED"}, status_code=403)
        if not 1 <= stage <= 7:
            raise WorkflowError("RUN_REQUEST_INVALID")
        configuration = RunRequest.from_dict(value, settings.max_files)
        return workflow.execute(stage, configuration, caller.principal_id)

    @app.get("/runs/{run_id}")
    def run_receipt(run_id: UUID):
        with workflow.lock:
            return receipt(get_run(run_id))

    @app.get("/inventories/{inventory_id}")
    def inventory_version(inventory_id: UUID):
        return {
            "inventory_id": str(inventory_id),
            "state_version": runtime.repository.get_inventory_version(inventory_id) or 0,
        }

    @app.get("/runs/{run_id}/review")
    def review_page(
        run_id: UUID,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=100),
    ):
        with workflow.lock:
            state = get_run(run_id)
            if state["completed_stage"] != 7:
                raise WorkflowError("REVIEW_STAGE_REQUIRED")
            return {
                "inventory_id": state["request"]["inventory_id"],
                "state_version": state["state_version"],
                "total_count": len(state["review_items"]),
                "items": state["review_items"][offset : offset + limit],
            }

    return app
