from agent_framework import tool
from osdu_agent_host.application import build_agent
from osdu_agent_host.mcp import call_mcp


def create_agent(settings, credential):
    @tool
    async def extract_detected_metadata(request_id: str, file_id: str) -> dict:
        """Extract one file using its observed format and registered deterministic parser."""
        return await call_mcp(
            settings,
            credential,
            "extraction",
            "extract_detected_metadata",
            request_id,
            {"file_id": file_id},
        )

    return build_agent(
        "extract-metadata", __file__, settings, credential, [extract_detected_metadata]
    )
