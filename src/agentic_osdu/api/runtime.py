"""ASGI entry point composed from the production deterministic-tool runtime."""

from agentic_osdu.api.app import bundled_web_directory, create_app
from agentic_osdu.runtime import create_runtime

runtime = create_runtime()
app = create_app(
    runtime.registry,
    web_directory=bundled_web_directory(),
)

__all__ = ["app", "runtime"]
