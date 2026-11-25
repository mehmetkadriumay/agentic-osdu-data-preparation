from mcp_servers.shared.transport import operation, server


def create_server(service):
    mcp = server("workspace-discovery")

    @mcp.tool()
    async def register_source_workspace(request_id: str) -> dict:
        """Register the operator-configured source as read-only; return its actual workspace ID."""
        return await operation(service, "register_source_workspace", request_id, {})

    @mcp.tool()
    async def discover_source_files(request_id: str, max_files: int = 100) -> dict:
        """Discover only the configured Data subtree without following links or junctions."""
        return await operation(
            service, "discover_source_files", request_id, {"max_files": max_files}
        )

    @mcp.tool()
    async def inspect_file_format(request_id: str, file_id: str) -> dict:
        """Read a bounded prefix and detect format evidence for one discovered file."""
        return await operation(service, "inspect_file_format", request_id, {"file_id": file_id})

    return mcp
