"""Deterministic, review-gated plans for WF-001 through WF-007."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agentic_osdu.agents.roles import AGENT_ROLES, AgentId
from agentic_osdu.tools.contracts import TOOL_REGISTRY


class WorkflowId(StrEnum):
    WF_001 = "WF-001"
    WF_002 = "WF-002"
    WF_003 = "WF-003"
    WF_004 = "WF-004"
    WF_005 = "WF-005"
    WF_006 = "WF-006"
    WF_007 = "WF-007"


class ExecutionAuthority(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


class StepCardinality(StrEnum):
    SINGLE = "single"
    FAN_OUT = "fan_out"
    JOIN = "join"


class StepInputPredicate(StrEnum):
    GENERATE_DRY_RUN = "generate_dry_run"
    GENERATE_WRITE = "generate_write"


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    step_id: str
    tool_id: str
    role: AgentId | None
    purpose: str
    authority: ExecutionAuthority = ExecutionAuthority.AGENT
    requires_approval: bool = False
    optional: bool = False
    depends_on: tuple[str, ...] = ()
    cardinality: StepCardinality = StepCardinality.SINGLE
    input_predicate: StepInputPredicate | None = None

    def __post_init__(self) -> None:
        if self.tool_id not in TOOL_REGISTRY:
            raise ValueError(f"{self.step_id} must use a registered tool")
        if self.authority is ExecutionAuthority.HUMAN:
            if self.role is not None:
                raise ValueError("human-authority steps cannot be assigned to an agent")
        elif self.role is None or self.tool_id not in AGENT_ROLES[self.role].allowed_tool_ids:
            raise ValueError(f"{self.role} is not allowed to invoke {self.tool_id}")
        if self.input_predicate is not None and self.tool_id != "TOOL-018":
            raise ValueError("generation input predicates apply only to TOOL-018")
        if self.cardinality is StepCardinality.FAN_OUT and not self.depends_on:
            raise ValueError("fan-out steps require at least one dependency")


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    workflow_id: WorkflowId
    steps: tuple[WorkflowStep, ...]

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("a workflow plan requires at least one step")
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise ValueError("workflow step IDs must be unique")

    @property
    def tool_ids(self) -> tuple[str, ...]:
        return tuple(step.tool_id for step in self.steps)

    def __getitem__(self, value: slice) -> WorkflowPlan:
        if not isinstance(value, slice):
            raise TypeError("workflow plans support slicing only")
        return WorkflowPlan(workflow_id=self.workflow_id, steps=self.steps[value])


def _step(
    number: int,
    tool_id: str,
    role: AgentId | None,
    purpose: str,
    *,
    human: bool = False,
    approval: bool = False,
    optional: bool = False,
    depends_on: tuple[str, ...] = (),
    cardinality: StepCardinality = StepCardinality.SINGLE,
    input_predicate: StepInputPredicate | None = None,
) -> WorkflowStep:
    return WorkflowStep(
        step_id=f"step-{number:02d}",
        tool_id=tool_id,
        role=role,
        purpose=purpose,
        authority=ExecutionAuthority.HUMAN if human else ExecutionAuthority.AGENT,
        requires_approval=approval or human,
        optional=optional,
        depends_on=depends_on,
        cardinality=cardinality,
        input_predicate=input_predicate,
    )


def build_workflow_plan(
    workflow_id: WorkflowId,
    *,
    extraction_tool_ids: tuple[str, ...] = (),
) -> WorkflowPlan:
    """Build a fixed, auditable tool sequence without executable domain behavior."""

    extraction_ids = tuple(dict.fromkeys(extraction_tool_ids))
    if workflow_id is not WorkflowId.WF_001 and extraction_ids:
        raise ValueError("extractor selection is valid only for WF-001")
    allowed_extractors = {f"TOOL-{number:03d}" for number in range(6, 14)}
    if any(tool_id not in allowed_extractors for tool_id in extraction_ids):
        raise ValueError("WF-001 extractors must be registered TOOL-006 through TOOL-013")

    if workflow_id is WorkflowId.WF_001:
        steps = [
            _step(1, "TOOL-001", AgentId.WORKSPACE_INTAKE, "Register the user-approved workspace."),
            _step(
                2,
                "TOOL-002",
                AgentId.WORKSPACE_INTAKE,
                "Discover bounded workspace files.",
                depends_on=("step-01",),
            ),
            _step(
                3,
                "TOOL-003",
                AgentId.WORKSPACE_INTAKE,
                "Read bounded file samples.",
                depends_on=("step-02",),
                cardinality=StepCardinality.FAN_OUT,
            ),
            _step(
                4,
                "TOOL-004",
                AgentId.DATA_CLASSIFICATION,
                "Detect candidate formats.",
                depends_on=("step-03",),
                cardinality=StepCardinality.FAN_OUT,
            ),
        ]
        for tool_id in extraction_ids:
            steps.append(
                _step(
                    len(steps) + 1,
                    tool_id,
                    AgentId.DATA_CLASSIFICATION,
                    "Extract compatible typed metadata.",
                    depends_on=("step-04",),
                    cardinality=StepCardinality.FAN_OUT,
                )
            )
        classification_dependencies = (
            "step-04",
            *(f"step-{number:02d}" for number in range(5, 5 + len(extraction_ids))),
        )
        steps.extend(
            (
                _step(
                    len(steps) + 1,
                    "TOOL-005",
                    AgentId.DATA_CLASSIFICATION,
                    "Classify from tool evidence.",
                    depends_on=classification_dependencies,
                    cardinality=StepCardinality.FAN_OUT,
                ),
                _step(
                    len(steps) + 2,
                    "TOOL-022",
                    AgentId.DATA_CLASSIFICATION,
                    "Persist versioned inventory results.",
                    depends_on=(
                        "step-02",
                        f"step-{4 + len(extraction_ids) + 1:02d}",
                    ),
                    cardinality=StepCardinality.JOIN,
                ),
            )
        )
        return WorkflowPlan(
            workflow_id,
            tuple(steps),
        )
    if workflow_id is WorkflowId.WF_002:
        return WorkflowPlan(
            workflow_id,
            (
                _step(1, "TOOL-014", AgentId.MANIFEST_ASSOCIATION, "Parse manifests."),
                _step(
                    2,
                    "TOOL-015",
                    AgentId.MANIFEST_ASSOCIATION,
                    "Extract records and references.",
                    depends_on=("step-01",),
                    cardinality=StepCardinality.FAN_OUT,
                ),
                _step(
                    3,
                    "TOOL-016",
                    AgentId.MANIFEST_ASSOCIATION,
                    "Match each file against the joined manifest index.",
                    depends_on=("step-01", "step-02"),
                    cardinality=StepCardinality.FAN_OUT,
                ),
                _step(
                    4,
                    "TOOL-023",
                    AgentId.MANIFEST_ASSOCIATION,
                    "Persist proposed associations.",
                    depends_on=("step-03",),
                    cardinality=StepCardinality.JOIN,
                ),
            ),
        )
    if workflow_id is WorkflowId.WF_003:
        return WorkflowPlan(
            workflow_id,
            (
                _step(1, "TOOL-017", AgentId.MANIFEST_LEARNING, "Learn approved examples."),
                _step(
                    2,
                    "TOOL-024",
                    AgentId.MANIFEST_LEARNING,
                    "Persist or activate a reviewed model version.",
                    approval=True,
                    depends_on=("step-01",),
                ),
            ),
        )
    if workflow_id is WorkflowId.WF_004:
        return WorkflowPlan(
            workflow_id,
            (
                _step(
                    1,
                    "TOOL-018",
                    AgentId.MANIFEST_GENERATION,
                    "Generate a dry-run candidate.",
                    input_predicate=StepInputPredicate.GENERATE_DRY_RUN,
                ),
                _step(
                    2,
                    "TOOL-020",
                    AgentId.VALIDATION_REVIEW,
                    "Validate schema conformance.",
                    depends_on=("step-01",),
                ),
                _step(
                    3,
                    "TOOL-028",
                    AgentId.VALIDATION_REVIEW,
                    "Prepare the candidate diff.",
                    depends_on=("step-01", "step-02"),
                    cardinality=StepCardinality.JOIN,
                ),
                _step(
                    4,
                    "TOOL-018",
                    AgentId.MANIFEST_GENERATION,
                    "Write the approved candidate.",
                    approval=True,
                    optional=True,
                    depends_on=("step-03",),
                    input_predicate=StepInputPredicate.GENERATE_WRITE,
                ),
                _step(
                    5,
                    "TOOL-029",
                    None,
                    "Record the user's review decision.",
                    human=True,
                    depends_on=("step-03",),
                ),
            ),
        )
    if workflow_id is WorkflowId.WF_005:
        return WorkflowPlan(
            workflow_id,
            (
                _step(
                    1,
                    "TOOL-025",
                    AgentId.JOB_SUPERVISOR,
                    "Create the approved batch job.",
                    approval=True,
                ),
                _step(
                    2,
                    "TOOL-019",
                    AgentId.MANIFEST_GENERATION,
                    "Generate missing candidates.",
                    depends_on=("step-01",),
                ),
                _step(
                    3,
                    "TOOL-020",
                    AgentId.VALIDATION_REVIEW,
                    "Validate each candidate.",
                    depends_on=("step-02",),
                    cardinality=StepCardinality.FAN_OUT,
                ),
                _step(
                    4,
                    "TOOL-022",
                    AgentId.MANIFEST_GENERATION,
                    "Persist candidate inventory state.",
                    depends_on=("step-02", "step-03"),
                    cardinality=StepCardinality.JOIN,
                ),
                _step(
                    5,
                    "TOOL-023",
                    AgentId.MANIFEST_GENERATION,
                    "Persist proposed associations.",
                    depends_on=("step-02", "step-03"),
                    cardinality=StepCardinality.JOIN,
                ),
                _step(
                    6,
                    "TOOL-027",
                    AgentId.VALIDATION_REVIEW,
                    "Prepare the batch review view.",
                    depends_on=("step-04", "step-05"),
                    cardinality=StepCardinality.JOIN,
                ),
            ),
        )
    if workflow_id is WorkflowId.WF_006:
        return WorkflowPlan(
            workflow_id,
            (
                _step(1, "TOOL-028", AgentId.VALIDATION_REVIEW, "Prepare manifest details."),
                _step(
                    2,
                    "TOOL-020",
                    AgentId.VALIDATION_REVIEW,
                    "Refresh stale validation.",
                    optional=True,
                ),
                _step(3, "TOOL-029", None, "Record the user's review decision.", human=True),
                _step(
                    4,
                    "TOOL-030",
                    None,
                    "Export an approved reviewed result.",
                    human=True,
                    optional=True,
                ),
            ),
        )
    if workflow_id is WorkflowId.WF_007:
        return _single_role_plan(workflow_id, ("TOOL-026",), AgentId.JOB_SUPERVISOR)
    raise ValueError(f"unsupported workflow: {workflow_id}")


def _single_role_plan(
    workflow_id: WorkflowId,
    tool_ids: tuple[str, ...],
    role: AgentId,
) -> WorkflowPlan:
    return WorkflowPlan(
        workflow_id,
        tuple(
            _step(index, tool_id, role, TOOL_REGISTRY[tool_id].purpose)
            for index, tool_id in enumerate(tool_ids, 1)
        ),
    )


__all__ = [
    "ExecutionAuthority",
    "StepCardinality",
    "StepInputPredicate",
    "WorkflowId",
    "WorkflowPlan",
    "WorkflowStep",
    "build_workflow_plan",
]
