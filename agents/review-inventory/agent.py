from agent_framework import tool
from osdu_agent_host.application import build_agent
from osdu_agent_host.mcp import call_mcp


def create_agent(settings, credential):
    @tool
    async def review_persisted_inventory(
        request_id: str, inventory_id: str, offset: int = 0, limit: int = 100
    ) -> dict:
        """Review one bounded page of an existing inventory, with metadata and failure evidence."""
        return await call_mcp(
            settings,
            credential,
            "inventory-review",
            "review_persisted_inventory",
            request_id,
            {"inventory_id": inventory_id, "offset": offset, "limit": limit},
        )

    return build_agent(
        "review-inventory", __file__, settings, credential, [review_persisted_inventory]
    )
