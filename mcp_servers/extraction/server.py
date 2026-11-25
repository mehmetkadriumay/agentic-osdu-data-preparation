from mcp_servers.shared.transport import operation, server


def create_server(service):
    mcp = server("extraction")

    @mcp.tool()
    async def extract_detected_metadata(request_id: str, file_id: str) -> dict:
        """Select the registered extractor from content evidence, never an extension alone."""
        return await operation(
            service, "extract_detected_metadata", request_id, {"file_id": file_id}
        )

    return mcp
