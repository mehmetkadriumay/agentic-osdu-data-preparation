from agent_framework import tool
from osdu_agent_host.application import build_agent
from osdu_agent_host.mcp import call_mcp


def create_agent(settings, credential):
    @tool
    async def register_source_workspace(request_id: str) -> dict:
        """Register the configured approved read-only source workspace."""
        return await call_mcp(
            settings,
            credential,
            "workspace-discovery",
            "register_source_workspace",
            request_id,
            {},
        )

    return build_agent(
        "approve-workspace", __file__, settings, credential, [register_source_workspace]
    )
