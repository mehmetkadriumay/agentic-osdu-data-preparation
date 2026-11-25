"""Hosting composition shared by otherwise independent Foundry services."""

from pathlib import Path

from .settings import Settings


def build_agent(name, source_file, settings, credential, tools):
    from agent_framework import Agent
    from agent_framework.foundry import FoundryChatClient

    instructions = Path(source_file).with_name("instructions.txt").read_text(encoding="utf-8")
    return Agent(
        client=FoundryChatClient(
            project_endpoint=settings.project_endpoint,
            model=settings.model_deployment,
            credential=credential,
        ),
        name=name,
        instructions=instructions,
        tools=tools,
        default_options={"store": False},
    )


def serve(factory) -> None:
    from agent_framework_foundry_hosting import ResponsesHostServer
    from azure.identity import DefaultAzureCredential

    settings = Settings.from_env()
    with DefaultAzureCredential() as credential:
        ResponsesHostServer(agent=lambda: factory(settings, credential)).run()
