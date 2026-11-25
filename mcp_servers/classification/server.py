from mcp_servers.shared.transport import operation, server


def create_server(service):
    mcp = server("classification")

    @mcp.tool()
    async def classify_observed_file(request_id: str, file_id: str) -> dict:
        """Classify a file with actual extraction references; missing fields stay unknown."""
        return await operation(service, "classify_observed_file", request_id, {"file_id": file_id})

    return mcp
