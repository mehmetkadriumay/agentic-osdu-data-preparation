from agent_framework import tool
from osdu_agent_host.application import build_agent
from osdu_agent_host.mcp import call_mcp


def create_agent(settings, credential):
    @tool
    async def classify_observed_file(request_id: str, file_id: str) -> dict:
        """Classify authoritative observations for one file without invented references."""
        return await call_mcp(
            settings,
            credential,
            "classification",
            "classify_observed_file",
            request_id,
            {"file_id": file_id},
        )

    return build_agent("classify-data", __file__, settings, credential, [classify_observed_file])
