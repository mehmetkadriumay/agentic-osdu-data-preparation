"""Project-owned registry adapter and bounded workflow executor."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from agentic_osdu.agents.roles import AgentId
from agentic_osdu.agents.workflows import (
    ExecutionAuthority,
    StepCardinality,
    StepInputPredicate,
    WorkflowId,
    WorkflowPlan,
    WorkflowStep,
)
from agentic_osdu.domain.models import ActorRef, TrustLevel
from agentic_osdu.tools.contracts import (
    TOOL_REGISTRY,
    GenerateManifestInput,
    ToolDefinition,
    ToolRequest,
    ToolResult,
    ToolResultStatus,
)

ToolHandler = Callable[..., Any]
Clock = Callable[[], datetime]


class OrchestrationError(RuntimeError):
    """Safe failure raised for an invalid plan, capability, or tool envelope."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ApprovalReceipt:
    """Immutable authorization minted by the trusted human-approval boundary."""

    receipt_id: UUID
    workflow_id: WorkflowId
    run_id: UUID
    workspace_id: UUID
    action: str
    request_id: UUID
    target_identity: str
    payload_sha256: str
    approved_by: ActorRef
    issued_at: datetime
    expires_at: datetime
    signature: str


class HumanApprovalBoundary:
    """Mint and validate workflow-bound receipts without trusting caller assertions."""

    def __init__(self, secret: bytes, *, clock: Clock | None = None) -> None:
        if len(secret) < 16:
            raise ValueError("approval boundary secret must contain at least 16 bytes")
        self._secret = bytes(secret)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._consumed: dict[UUID, str] = {}

    def issue(
        self,
        *,
        workflow_id: WorkflowId,
        run_id: UUID,
        step: WorkflowStep,
        request: ToolRequest[Any],
        approved_by: ActorRef,
        expires_at: datetime,
    ) -> ApprovalReceipt:
        if _agent_id(approved_by.actor_id) is not None:
            raise OrchestrationError(
                "SELF_APPROVAL_DENIED",
                "An authenticated agent cannot mint a human approval receipt.",
            )
        now = self._clock()
        if expires_at <= now:
            raise OrchestrationError(
                "APPROVAL_RECEIPT_EXPIRED",
                "Approval expiry must be later than its issue time.",
            )
        receipt_id = uuid4()
        action = _approval_action(step)
        target_identity = _target_identity(request.input)
        payload_sha256 = _payload_sha256(request.input)
        values: dict[str, Any] = {
            "receipt_id": receipt_id,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "workspace_id": request.workspace_id,
            "action": action,
            "request_id": request.request_id,
            "target_identity": target_identity,
            "payload_sha256": payload_sha256,
            "approved_by": approved_by,
            "issued_at": now,
            "expires_at": expires_at,
        }
        signature = self._sign(values)
        return ApprovalReceipt(
            receipt_id=receipt_id,
            workflow_id=workflow_id,
            run_id=run_id,
            workspace_id=request.workspace_id,
            action=action,
            request_id=request.request_id,
            target_identity=target_identity,
            payload_sha256=payload_sha256,
            approved_by=approved_by,
            issued_at=now,
            expires_at=expires_at,
            signature=signature,
        )

    def authorize(
        self,
        receipt: ApprovalReceipt,
        workflow_id: WorkflowId,
        run_id: UUID,
        step: WorkflowStep,
        request: ToolRequest[Any],
    ) -> None:
        values = {
            field: getattr(receipt, field)
            for field in (
                "receipt_id",
                "workflow_id",
                "run_id",
                "workspace_id",
                "action",
                "request_id",
                "target_identity",
                "payload_sha256",
                "approved_by",
                "issued_at",
                "expires_at",
            )
        }
        if not hmac.compare_digest(receipt.signature, self._sign(values)):
            raise OrchestrationError(
                "APPROVAL_RECEIPT_INVALID",
                "The approval receipt was not minted by the trusted boundary.",
            )
        expected = _approval_binding(workflow_id, run_id, step, request)
        actual = _approval_binding(
            receipt.workflow_id,
            receipt.run_id,
            step,
            request,
            action=receipt.action,
            workspace_id=receipt.workspace_id,
            request_id=receipt.request_id,
            target_identity=receipt.target_identity,
            payload_sha256=receipt.payload_sha256,
        )
        if actual != expected:
            raise OrchestrationError(
                "APPROVAL_BINDING_MISMATCH",
                "The receipt is not bound to this run, workspace, action, request, and payload.",
            )
        if receipt.expires_at <= self._clock():
            raise OrchestrationError(
                "APPROVAL_RECEIPT_EXPIRED",
                "The approval receipt has expired.",
            )
        previous = self._consumed.setdefault(receipt.receipt_id, expected)
        if previous != expected:
            raise OrchestrationError(
                "APPROVAL_REPLAY_DENIED",
                "An approval receipt cannot be replayed for a different operation.",
            )

    def _sign(self, values: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            _jsonable(values),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hmac.new(self._secret, encoded, hashlib.sha256).hexdigest()


@runtime_checkable
class AgentAuthenticator(Protocol):
    """Trusted boundary that resolves an actor reference to a typed agent role."""

    def authenticate(self, actor: ActorRef) -> AgentId | None: ...


class StaticAgentAuthenticator:
    """Authenticate only explicitly configured actor-to-role bindings."""

    def __init__(self, identities: Mapping[str, AgentId] | None = None) -> None:
        self._identities = dict(identities or {agent_id.value: agent_id for agent_id in AgentId})

    def authenticate(self, actor: ActorRef) -> AgentId | None:
        return self._identities.get(actor.actor_id)


@runtime_checkable
class ToolInvoker(Protocol):
    """Framework-neutral boundary for invoking registered typed tools."""

    def invoke(self, tool_id: str, request: ToolRequest[Any]) -> ToolResult[Any]: ...


class ToolRegistryAdapter:
    """Bind project-owned tool metadata to injected deterministic handlers."""

    def __init__(self, handlers: Mapping[str, ToolHandler]) -> None:
        unknown = set(handlers).difference(TOOL_REGISTRY)
        if unknown:
            raise OrchestrationError(
                "UNREGISTERED_TOOL",
                f"Handlers include unregistered tool IDs: {sorted(unknown)}",
            )
        self._handlers = dict(handlers)

    def definition(self, tool_id: str) -> ToolDefinition:
        definition = TOOL_REGISTRY.get(tool_id)
        if definition is None:
            raise OrchestrationError("UNREGISTERED_TOOL", "The requested tool is not registered.")
        return definition

    def invoke(
        self,
        tool_id: str,
        request: ToolRequest[Any],
    ) -> ToolResult[Any]:
        definition = self.definition(tool_id)
        handler = self._handlers.get(tool_id)
        if handler is None:
            raise OrchestrationError(
                "UNREGISTERED_TOOL",
                "The deterministic tool has no registered executable handler.",
            )
        if not isinstance(request, ToolRequest) or not isinstance(
            request.input, definition.input_model
        ):
            raise OrchestrationError(
                "TOOL_INPUT_INVALID",
                f"{tool_id} requires {definition.input_model.__name__}.",
            )
        result = handler(request)
        if not isinstance(result, ToolResult):
            raise OrchestrationError(
                "TOOL_RESULT_INVALID",
                "The tool handler did not return a typed ToolResult.",
            )
        output_invalid = result.output is not None and not isinstance(
            result.output,
            definition.output_model,
        )
        if (
            result.request_id != request.request_id
            or result.tool_id != tool_id
            or result.tool_version != definition.version
            or output_invalid
        ):
            raise OrchestrationError(
                "TOOL_RESULT_INVALID",
                "The tool result does not match its request or registered contract.",
            )
        _enforce_trust_policy(definition, request, result)
        return result


@dataclass(frozen=True, slots=True)
class StepOutcome:
    step_id: str
    attempts: int
    result: ToolResult[Any]
    binding_key: str = ""


@dataclass(frozen=True, slots=True)
class BoundStepRequest:
    """One deterministically keyed request produced for a workflow step."""

    binding_key: str
    request: ToolRequest[Any]

    def __post_init__(self) -> None:
        if not self.binding_key:
            raise ValueError("binding_key must not be empty")


@dataclass(frozen=True, slots=True)
class StepRequestContext:
    """Prior typed outcomes made available to a deterministic request deriver."""

    plan: WorkflowPlan
    step: WorkflowStep
    dependencies: tuple[StepOutcome, ...]


RequestDeriver = Callable[[StepRequestContext], tuple[BoundStepRequest, ...]]


@dataclass(frozen=True, slots=True)
class NarrativeCitation:
    tool_id: str
    request_id: UUID
    evidence_ids: tuple[UUID, ...]
    provenance_ids: tuple[UUID, ...]
    trust_level: TrustLevel


@dataclass(frozen=True, slots=True)
class WorkflowOutcome:
    workflow_id: str
    succeeded: bool
    steps: tuple[StepOutcome, ...]
    narrative: tuple[NarrativeCitation, ...]


@runtime_checkable
class WorkflowExecutor(Protocol):
    """Framework-neutral boundary for executing an approved workflow plan."""

    def execute(
        self,
        plan: WorkflowPlan,
        step_requests: Mapping[str, ToolRequest[Any]],
        *,
        run_id: UUID | None = None,
        approval_receipts: Mapping[str, ApprovalReceipt] | None = None,
        request_derivers: Mapping[str, RequestDeriver] | None = None,
    ) -> WorkflowOutcome: ...


class Orchestrator:
    """Execute deterministic plans without acquiring domain implementation access."""

    def __init__(
        self,
        registry: ToolInvoker,
        *,
        max_attempts: int = 2,
        approval_boundary: HumanApprovalBoundary | None = None,
        agent_authenticator: AgentAuthenticator | None = None,
    ) -> None:
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between one and ten")
        self._registry = registry
        self._max_attempts = max_attempts
        self._approval_boundary = approval_boundary
        self._agent_authenticator = agent_authenticator or StaticAgentAuthenticator()

    def execute(
        self,
        plan: WorkflowPlan,
        step_requests: Mapping[str, ToolRequest[Any]],
        *,
        run_id: UUID | None = None,
        approval_receipts: Mapping[str, ApprovalReceipt] | None = None,
        request_derivers: Mapping[str, RequestDeriver] | None = None,
    ) -> WorkflowOutcome:
        effective_run_id = run_id or uuid4()
        receipts = approval_receipts or {}
        derivers = request_derivers or {}
        outcomes: list[StepOutcome] = []
        citations: list[NarrativeCitation] = []
        for step in plan.steps:
            bound_requests = self._requests_for_step(plan, step, step_requests, derivers, outcomes)
            if not bound_requests:
                if step.optional:
                    continue
                raise OrchestrationError(
                    "WORKFLOW_INPUT_MISSING",
                    f"No typed request was supplied for {step.step_id}.",
                )
            if step.cardinality is StepCardinality.SINGLE and len(bound_requests) != 1:
                raise OrchestrationError(
                    "WORKFLOW_CARDINALITY_INVALID",
                    f"{step.step_id} requires exactly one request.",
                )
            for bound in bound_requests:
                self._authorize_step(
                    plan.workflow_id,
                    effective_run_id,
                    step,
                    bound.request,
                    receipts.get(step.step_id),
                )
                result, attempts = self._invoke_with_retry(step.tool_id, bound.request)
                outcomes.append(StepOutcome(step.step_id, attempts, result, bound.binding_key))
                citations.append(
                    NarrativeCitation(
                        tool_id=result.tool_id,
                        request_id=result.request_id,
                        evidence_ids=tuple(item.evidence_id for item in result.evidence),
                        provenance_ids=tuple(item.provenance_id for item in result.provenance),
                        trust_level=result.trust_level,
                    )
                )
                if result.status is not ToolResultStatus.SUCCEEDED:
                    return WorkflowOutcome(
                        plan.workflow_id.value,
                        False,
                        tuple(outcomes),
                        tuple(citations),
                    )
        return WorkflowOutcome(plan.workflow_id.value, True, tuple(outcomes), tuple(citations))

    @staticmethod
    def _requests_for_step(
        plan: WorkflowPlan,
        step: WorkflowStep,
        step_requests: Mapping[str, ToolRequest[Any]],
        derivers: Mapping[str, RequestDeriver],
        outcomes: list[StepOutcome],
    ) -> tuple[BoundStepRequest, ...]:
        direct = step_requests.get(step.step_id)
        if direct is not None:
            return (BoundStepRequest(step.step_id, direct),)
        deriver = derivers.get(step.step_id)
        if deriver is None:
            return ()
        dependencies = tuple(outcome for outcome in outcomes if outcome.step_id in step.depends_on)
        missing = set(step.depends_on).difference(outcome.step_id for outcome in dependencies)
        if missing:
            raise OrchestrationError(
                "WORKFLOW_DEPENDENCY_MISSING",
                f"{step.step_id} is missing completed dependencies: {sorted(missing)}",
            )
        derived = deriver(StepRequestContext(plan, step, dependencies))
        if not isinstance(derived, tuple) or any(
            not isinstance(item, BoundStepRequest) for item in derived
        ):
            raise OrchestrationError(
                "WORKFLOW_DERIVATION_INVALID",
                "A request deriver must return a tuple of BoundStepRequest values.",
            )
        keys = tuple(item.binding_key for item in derived)
        if len(set(keys)) != len(keys):
            raise OrchestrationError(
                "WORKFLOW_DERIVATION_INVALID",
                "Derived request binding keys must be unique within a step.",
            )
        return derived

    def _invoke_with_retry(
        self,
        tool_id: str,
        request: ToolRequest[Any],
    ) -> tuple[ToolResult[Any], int]:
        attempts = 0
        while True:
            attempts += 1
            result = self._registry.invoke(tool_id, request)
            if not self._may_retry(result) or attempts >= self._max_attempts:
                return result, attempts

    @staticmethod
    def _may_retry(result: ToolResult[Any]) -> bool:
        return (
            result.status is ToolResultStatus.FAILED
            and bool(result.errors)
            and all(error.retryable for error in result.errors)
            and not result.side_effects
        )

    def _authorize_step(
        self,
        workflow_id: WorkflowId,
        run_id: UUID,
        step: WorkflowStep,
        request: ToolRequest[Any],
        receipt: ApprovalReceipt | None,
    ) -> None:
        actor_role = self._agent_authenticator.authenticate(request.actor)
        if step.authority is ExecutionAuthority.AGENT and actor_role is not step.role:
            raise OrchestrationError(
                "AGENT_ROLE_MISMATCH",
                f"{step.step_id} requires authenticated role {step.role}.",
            )
        if step.authority is ExecutionAuthority.HUMAN and actor_role is not None:
            raise OrchestrationError(
                "SELF_APPROVAL_DENIED",
                "An agent cannot execute a human review or export decision.",
            )
        _enforce_input_predicate(step, request)
        if step.requires_approval:
            if receipt is None or self._approval_boundary is None:
                raise OrchestrationError(
                    "HUMAN_APPROVAL_REQUIRED",
                    f"{step.step_id} requires a trusted human approval receipt.",
                )
            self._approval_boundary.authorize(receipt, workflow_id, run_id, step, request)


def _agent_id(actor_id: str) -> AgentId | None:
    try:
        return AgentId(actor_id)
    except ValueError:
        return None


def _enforce_input_predicate(step: WorkflowStep, request: ToolRequest[Any]) -> None:
    predicate = step.input_predicate
    if predicate is None:
        return
    if not isinstance(request.input, GenerateManifestInput):
        raise OrchestrationError(
            "STEP_INPUT_POLICY_DENIED",
            f"{step.step_id} requires GenerateManifestInput.",
        )
    expected_dry_run = predicate is StepInputPredicate.GENERATE_DRY_RUN
    if request.input.dry_run is not expected_dry_run:
        raise OrchestrationError(
            "STEP_INPUT_POLICY_DENIED",
            f"{step.step_id} requires dry_run={expected_dry_run}.",
        )


_TRUST_RANK = {
    TrustLevel.UNTRUSTED: 0,
    TrustLevel.HEURISTIC: 1,
    TrustLevel.DERIVED: 2,
    TrustLevel.VERIFIED: 3,
    TrustLevel.AUTHORITATIVE: 4,
}


def _enforce_trust_policy(
    definition: ToolDefinition,
    request: ToolRequest[Any],
    result: ToolResult[Any],
) -> None:
    if definition.default_trust is not None:
        if _TRUST_RANK[result.trust_level] > _TRUST_RANK[definition.default_trust]:
            raise OrchestrationError(
                "TOOL_TRUST_POLICY_VIOLATION",
                f"{definition.tool_id} cannot exceed {definition.default_trust.value} trust.",
            )
        return
    inherited = tuple(_trust_levels(request.input)) + tuple(_trust_levels(result.output))
    maximum = min(inherited, key=_TRUST_RANK.__getitem__) if inherited else TrustLevel.UNTRUSTED
    if _TRUST_RANK[result.trust_level] > _TRUST_RANK[maximum]:
        raise OrchestrationError(
            "TOOL_TRUST_POLICY_VIOLATION",
            f"{definition.tool_id} cannot elevate inherited trust.",
        )


def _trust_levels(value: Any) -> list[TrustLevel]:
    found: list[TrustLevel] = []
    seen: set[int] = set()

    def visit(item: Any) -> None:
        if id(item) in seen:
            return
        seen.add(id(item))
        if isinstance(item, TrustLevel):
            found.append(item)
        elif isinstance(item, Mapping):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (tuple, list, set, frozenset)):
            for nested in item:
                visit(nested)
        elif hasattr(item, "__dict__"):
            for nested in vars(item).values():
                visit(nested)

    visit(value)
    return found


def _payload_sha256(value: Any) -> str:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _target_identity(value: Any) -> str:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else {}
    identities = {
        key: item
        for key, item in payload.items()
        if key.endswith(("_id", "_version", "_sha256")) or key in {"decision", "action"}
    }
    return hashlib.sha256(
        json.dumps(identities or payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _approval_action(step: WorkflowStep) -> str:
    return f"{step.tool_id}:{step.step_id}"


def _approval_binding(
    workflow_id: WorkflowId,
    run_id: UUID,
    step: WorkflowStep,
    request: ToolRequest[Any],
    *,
    action: str | None = None,
    workspace_id: UUID | None = None,
    request_id: UUID | None = None,
    target_identity: str | None = None,
    payload_sha256: str | None = None,
) -> str:
    values = (
        workflow_id.value,
        str(run_id),
        str(workspace_id or request.workspace_id),
        action or _approval_action(step),
        str(request_id or request.request_id),
        target_identity or _target_identity(request.input),
        payload_sha256 or _payload_sha256(request.input),
    )
    return "\x1f".join(values)


def _jsonable(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: (
            value.model_dump(mode="json")
            if hasattr(value, "model_dump")
            else value.value
            if isinstance(value, WorkflowId)
            else value.isoformat()
            if isinstance(value, datetime)
            else str(value)
            if isinstance(value, UUID)
            else value
        )
        for key, value in values.items()
    }


__all__ = [
    "AgentAuthenticator",
    "ApprovalReceipt",
    "BoundStepRequest",
    "HumanApprovalBoundary",
    "NarrativeCitation",
    "OrchestrationError",
    "Orchestrator",
    "StaticAgentAuthenticator",
    "StepOutcome",
    "StepRequestContext",
    "ToolInvoker",
    "ToolRegistryAdapter",
    "WorkflowExecutor",
    "WorkflowOutcome",
]
