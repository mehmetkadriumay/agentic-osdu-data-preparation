from agent_framework import tool
from osdu_agent_host.application import build_agent
from osdu_agent_host.mcp import call_mcp


def create_agent(settings, credential):
    @tool
    async def persist_observed_inventory(
        request_id: str,
        file_ids: list[str],
        inventory_id: str | None = None,
        expected_state_version: int = 0,
    ) -> dict:
        """Commit observed files at a protected barrier; the server allocates new inventory IDs."""
        return await call_mcp(
            settings,
            credential,
            "inventory-review",
            "persist_observed_inventory",
            request_id,
            {
                "file_ids": file_ids,
                "inventory_id": inventory_id,
                "expected_state_version": expected_state_version,
            },
        )

    return build_agent(
        "persist-inventory", __file__, settings, credential, [persist_observed_inventory]
    )
