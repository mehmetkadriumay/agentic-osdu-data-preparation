from agent_framework import tool
from osdu_agent_host.application import build_agent
from osdu_agent_host.mcp import call_mcp


def create_agent(settings, credential):
    @tool
    async def account_file_failures(request_id: str, file_ids: list[str]) -> dict:
        """Account for failed or missing observations on a selected set of files."""
        return await call_mcp(
            settings,
            credential,
            "inventory-review",
            "account_file_failures",
            request_id,
            {"file_ids": file_ids},
        )

    return build_agent("account-failures", __file__, settings, credential, [account_file_failures])
