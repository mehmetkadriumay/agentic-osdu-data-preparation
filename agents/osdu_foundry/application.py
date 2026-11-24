"""Host one bounded stage without giving the model arbitrary HTTP or MCP access."""

import asyncio
from uuid import UUID

import httpx
from agent_framework import Agent, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential

from .settings import Settings
from .stages import get_stage, instructions


def create_agent(settings: Settings, credential: DefaultAzureCredential) -> Agent:
    stage = get_stage(settings.stage)

    @tool
    async def run_stage(
        run_id: str,
        inventory_id: str,
        expected_state_version: int = 0,
        max_files: int = 100,
    ) -> str:
        """Execute only this agent's configured stage; return an authoritative receipt.

        All arguments must be copied from the caller's run configuration. Zero is
        the expected version only for a new inventory, not an observed version.
        """
        payload = {
            "run_id": str(UUID(run_id)),
            "inventory_id": str(UUID(inventory_id)),
            "expected_state_version": expected_state_version,
            "max_files": max_files,
        }
        token = await asyncio.to_thread(credential.get_token, settings.backend_scope)
        async with httpx.AsyncClient(timeout=1800, follow_redirects=False) as client:
            response = await client.post(
                f"{settings.backend_url}/stages/{stage.number}",
                json=payload,
                headers={"Authorization": f"Bearer {token.token}"},
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"Backend refused stage {stage.number} (HTTP {response.status_code})"
            )
        return response.text

    client = FoundryChatClient(
        project_endpoint=settings.project_endpoint,
        model=settings.model_deployment,
        credential=credential,
    )
    return Agent(
        client=client,
        name=stage.name,
        instructions=instructions(stage),
        tools=[run_stage],
        default_options={"store": False},
    )


def main() -> None:
    settings = Settings.from_env()
    with DefaultAzureCredential() as credential:
        ResponsesHostServer(agent=create_agent(settings, credential)).run()
