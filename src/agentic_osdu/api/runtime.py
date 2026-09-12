"""ASGI entry point composed from the production deterministic-tool runtime."""

from agentic_osdu.api.app import bundled_web_directory, create_app
from agentic_osdu.mcp_server import create_mcp_server
from agentic_osdu.runtime import create_runtime

runtime = create_runtime()
mcp_server = create_mcp_server(runtime.registry)
app = create_app(
    runtime.registry,
    web_directory=bundled_web_directory(),
    mcp_server=mcp_server,
)

__all__ = ["app", "mcp_server", "runtime"]
