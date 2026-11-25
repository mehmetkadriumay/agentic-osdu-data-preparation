"""Compose real MCP endpoints over one protected filesystem capability authority."""

import asyncio
import os
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

from agentic_osdu.runtime import create_runtime
from mcp_servers.classification.server import create_server as classification_server
from mcp_servers.extraction.server import create_server as extraction_server
from mcp_servers.inventory_review.server import create_server as inventory_server
from mcp_servers.shared.gateway import ToolGateway
from mcp_servers.shared.ledger import SqlLedger
from mcp_servers.shared.service import SERVER_ROLES, OperationService
from mcp_servers.shared.transport import caller_context
from mcp_servers.workspace_discovery.server import create_server as discovery_server

from .auth import AccessDenied, EntraAuthenticator
from .settings import Settings


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
    service = OperationService(
        ToolGateway(runtime),
        ledger,
        str(settings.source_root),
        settings.scan_subpath,
        settings.max_files,
    )
    auth = EntraAuthenticator(settings)
    servers = {
        "workspace-discovery": discovery_server(service),
        "extraction": extraction_server(service),
        "classification": classification_server(service),
        "inventory-review": inventory_server(service),
    }
    security = TransportSecuritySettings(
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=[],
    )
    mounts = {
        name: server.streamable_http_app(
            streamable_http_path="/",
            json_response=True,
            stateless_http=True,
            transport_security=security,
        )
        for name, server in servers.items()
    }

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            async with AsyncExitStack() as stack:
                for server in servers.values():
                    await stack.enter_async_context(server.session_manager.run())
                yield
        finally:
            runtime.database.dispose()

    app = FastAPI(
        title="OSDU capability MCP host",
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
            caller = await asyncio.to_thread(
                auth.authenticate,
                request.headers.get("authorization", ""),
            )
        except AccessDenied:
            return JSONResponse(
                {"code": "ACCESS_DENIED"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        path = request.url.path.split("/")
        if len(path) >= 3 and path[1] == "mcp":
            if caller.role not in SERVER_ROLES.get(path[2], ()):
                return JSONResponse({"code": "CAPABILITY_DENIED"}, status_code=403)
        elif not caller.reader:
            return JSONResponse({"code": "READER_ACCESS_REQUIRED"}, status_code=403)
        if request.method == "POST":
            try:
                length = int(request.headers["content-length"])
            except (KeyError, ValueError):
                return JSONResponse({"code": "CONTENT_LENGTH_REQUIRED"}, status_code=411)
            if not 0 <= length <= 1_048_576:
                return JSONResponse({"code": "REQUEST_TOO_LARGE"}, status_code=413)
        token = caller_context.set(caller)
        try:
            return await call_next(request)
        except Exception:
            return JSONResponse({"code": "MCP_OPERATION_FAILED"}, status_code=500)
        finally:
            caller_context.reset(token)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/operations/{request_id}")
    def operation_receipt(request_id: UUID):
        result = ledger.get(str(request_id))
        if result is None:
            return JSONResponse({"code": "OPERATION_NOT_FOUND"}, status_code=404)
        return result

    @app.get("/inventories/{inventory_id}")
    def inventory_version(inventory_id: UUID):
        return {
            "inventory_id": str(inventory_id),
            "state_version": runtime.repository.get_inventory_version(inventory_id) or 0,
        }

    for name, mounted in mounts.items():
        app.mount(f"/mcp/{name}", mounted, name=name)
    return app
