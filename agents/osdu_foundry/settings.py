"""Dependency-free configuration shared with the deployment clients."""

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from .stages import get_stage


def https_url(value: str, *, root_only: bool = False) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or (root_only and (parsed.query or parsed.path not in {"", "/"}))
        or any(character.isspace() for character in value)
    ):
        raise ValueError("An absolute HTTPS endpoint without credentials is required")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    stage: int
    project_endpoint: str
    model_deployment: str
    backend_url: str
    backend_scope: str

    @classmethod
    def from_env(cls) -> "Settings":
        stage = int(os.environ["OSDU_STAGE"])
        get_stage(stage)
        model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"].strip()
        scope = os.environ["OSDU_BACKEND_SCOPE"].strip()
        if not model or not scope.endswith("/.default") or any(c.isspace() for c in scope):
            raise ValueError("A model deployment and backend /.default token scope are required")
        return cls(
            stage=stage,
            project_endpoint=https_url(os.environ["FOUNDRY_PROJECT_ENDPOINT"]),
            model_deployment=model,
            backend_url=https_url(os.environ["OSDU_BACKEND_URL"], root_only=True),
            backend_scope=scope,
        )
