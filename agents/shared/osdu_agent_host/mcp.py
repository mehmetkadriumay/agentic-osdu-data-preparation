"""One authenticated MCP lifecycle per call; tokens and sessions are never shared."""

import asyncio
from uuid import UUID


async def call_mcp(
    settings, credential, server: str, operation: str, request_id: str, arguments: dict
) -> dict:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    request_id = str(UUID(request_id))
    token = await asyncio.to_thread(credential.get_token, settings.backend_scope)
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token.token}"},
        timeout=httpx2.Timeout(30, read=1800),
        follow_redirects=False,
    ) as http:
        transport = streamable_http_client(
            f"{settings.backend_url}/mcp/{server}/",
            http_client=http,
        )
        async with Client(transport) as client:
            result = await client.call_tool(
                operation,
                {"request_id": request_id, **arguments},
            )
    receipt = result.structured_content
    if result.is_error or not isinstance(receipt, dict):
        raise RuntimeError("The MCP server did not return an operation receipt")
    if receipt.get("request_id") != request_id or receipt.get("operation") != operation:
        raise RuntimeError("The MCP receipt does not match the requested operation")
    return receipt
