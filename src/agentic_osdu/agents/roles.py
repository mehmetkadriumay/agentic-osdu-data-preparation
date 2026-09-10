"""Static role capabilities for AGENT-001 through AGENT-007."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from agentic_osdu.tools.contracts import TOOL_REGISTRY


class AgentId(StrEnum):
    WORKSPACE_INTAKE = "AGENT-001"
    DATA_CLASSIFICATION = "AGENT-002"
    MANIFEST_ASSOCIATION = "AGENT-003"
    MANIFEST_LEARNING = "AGENT-004"
    MANIFEST_GENERATION = "AGENT-005"
    VALIDATION_REVIEW = "AGENT-006"
    JOB_SUPERVISOR = "AGENT-007"
    AGENT_001 = WORKSPACE_INTAKE
    AGENT_002 = DATA_CLASSIFICATION
    AGENT_003 = MANIFEST_ASSOCIATION
    AGENT_004 = MANIFEST_LEARNING
    AGENT_005 = MANIFEST_GENERATION
    AGENT_006 = VALIDATION_REVIEW
    AGENT_007 = JOB_SUPERVISOR


@dataclass(frozen=True, slots=True)
class AgentRole:
    agent_id: AgentId
    name: str
    allowed_tool_ids: frozenset[str]
    responsibilities: tuple[str, ...]
    prohibitions: tuple[str, ...]

    def __post_init__(self) -> None:
        unknown = self.allowed_tool_ids.difference(TOOL_REGISTRY)
        if unknown:
            raise ValueError(f"{self.agent_id} contains unregistered tools: {sorted(unknown)}")
        if "TOOL-029" in self.allowed_tool_ids:
            raise ValueError("review decisions are a human authority, not an agent capability")


_COMMON_PROHIBITIONS = (
    "No direct filesystem, parser, schema-validator, state, or database implementation access.",
    "No unregistered tool invocation or record submission.",
    "No trust level upgrades beyond deterministic tool results.",
)


def _role(
    agent_id: AgentId,
    name: str,
    tools: tuple[str, ...],
    responsibilities: tuple[str, ...],
    *prohibitions: str,
) -> AgentRole:
    return AgentRole(
        agent_id=agent_id,
        name=name,
        allowed_tool_ids=frozenset(tools),
        responsibilities=responsibilities,
        prohibitions=(*_COMMON_PROHIBITIONS, *prohibitions),
    )


AGENT_ROLES: Final = MappingProxyType(
    {
        AgentId.WORKSPACE_INTAKE: _role(
            AgentId.WORKSPACE_INTAKE,
            "Workspace Intake Agent",
            ("TOOL-001", "TOOL-002", "TOOL-003"),
            ("Register approved roots.", "Discover and sample approved workspace files."),
            "Must not bypass root policy.",
        ),
        AgentId.DATA_CLASSIFICATION: _role(
            AgentId.DATA_CLASSIFICATION,
            "Data Classification Agent",
            (*tuple(f"TOOL-{number:03d}" for number in range(3, 14)), "TOOL-022"),
            ("Detect formats.", "Extract metadata.", "Classify and persist inventory."),
            "Must not invent metadata absent from tool output.",
        ),
        AgentId.MANIFEST_ASSOCIATION: _role(
            AgentId.MANIFEST_ASSOCIATION,
            "Manifest Association Agent",
            ("TOOL-014", "TOOL-015", "TOOL-016", "TOOL-023"),
            ("Parse manifests.", "Extract references.", "Propose evidence-backed matches."),
            "Must not approve heuristic associations.",
        ),
        AgentId.MANIFEST_LEARNING: _role(
            AgentId.MANIFEST_LEARNING,
            "Manifest Learning Agent",
            ("TOOL-017", "TOOL-024"),
            ("Learn from reviewed pairs.", "Request versioned model activation."),
            "Must not learn from generated manifests or rejected associations.",
        ),
        AgentId.MANIFEST_GENERATION: _role(
            AgentId.MANIFEST_GENERATION,
            "Manifest Generation Agent",
            ("TOOL-018", "TOOL-019", "TOOL-022", "TOOL-023"),
            ("Generate review-required candidates.", "Persist candidate workflow state."),
            "Must not approve generated candidates.",
        ),
        AgentId.VALIDATION_REVIEW: _role(
            AgentId.VALIDATION_REVIEW,
            "Validation and Review Agent",
            ("TOOL-020", "TOOL-027", "TOOL-028"),
            ("Validate schema conformance.", "Prepare evidence-backed review views."),
            "Must not claim schema validity proves business correctness.",
        ),
        AgentId.JOB_SUPERVISOR: _role(
            AgentId.JOB_SUPERVISOR,
            "Job Supervisor Agent",
            ("TOOL-025", "TOOL-026"),
            ("Create and supervise jobs.", "Report progress and request cancellation."),
            "Must not mutate domain state outside typed job tools.",
        ),
    }
)

__all__ = ["AGENT_ROLES", "AgentId", "AgentRole"]
