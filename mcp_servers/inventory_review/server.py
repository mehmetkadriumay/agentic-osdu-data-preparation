from mcp_servers.shared.transport import operation, server


def create_server(service):
    mcp = server("inventory-review")

    @mcp.tool()
    async def account_file_failures(request_id: str, file_ids: list[str]) -> dict:
        """Report per-file missing results and failures without inventing metadata."""
        return await operation(service, "account_file_failures", request_id, {"file_ids": file_ids})

    @mcp.tool()
    async def persist_observed_inventory(
        request_id: str,
        file_ids: list[str],
        inventory_id: str | None = None,
        expected_state_version: int = 0,
    ) -> dict:
        """Commit observations at a single-writer barrier; return the actual inventory/version."""
        return await operation(
            service,
            "persist_observed_inventory",
            request_id,
            {
                "file_ids": file_ids,
                "inventory_id": inventory_id,
                "expected_state_version": expected_state_version,
            },
        )

    @mcp.tool()
    async def review_persisted_inventory(
        request_id: str, inventory_id: str, offset: int = 0, limit: int = 100
    ) -> dict:
        """Review a persisted inventory independently, including metadata and failure evidence."""
        return await operation(
            service,
            "review_persisted_inventory",
            request_id,
            {
                "inventory_id": inventory_id,
                "offset": offset,
                "limit": limit,
            },
        )

    return mcp
