from agent_framework import tool
from osdu_agent_host.application import build_agent
from osdu_agent_host.mcp import call_mcp


def create_agent(settings, credential):
    @tool
    async def discover_source_files(request_id: str, max_files: int = 100) -> dict:
        """Discover a bounded set of files within the configured Data-only scope."""
        return await call_mcp(
            settings,
            credential,
            "workspace-discovery",
            "discover_source_files",
            request_id,
            {"max_files": max_files},
        )

    @tool
    async def inspect_file_format(request_id: str, file_id: str) -> dict:
        """Sample and detect the format of one previously discovered file."""
        return await call_mcp(
            settings,
            credential,
            "workspace-discovery",
            "inspect_file_format",
            request_id,
            {"file_id": file_id},
        )

    return build_agent(
        "discover-formats",
        __file__,
        settings,
        credential,
        [discover_source_files, inspect_file_format],
    )
