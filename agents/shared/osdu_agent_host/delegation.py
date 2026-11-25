"""Bounded delegation to allowlisted Foundry agents with backend receipt verification."""

import asyncio
import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from .settings import https_url

AGENT_OPERATIONS = {
    "approve-workspace": {"register_source_workspace"},
    "discover-formats": {"discover_source_files", "inspect_file_format"},
    "extract-metadata": {"extract_detected_metadata"},
    "classify-data": {"classify_observed_file"},
    "account-failures": {"account_file_failures"},
    "persist-inventory": {"persist_observed_inventory"},
    "review-inventory": {"review_persisted_inventory"},
}
DEFAULT_ARGUMENTS = {
    "register_source_workspace": {},
    "discover_source_files": {"max_files": 100},
    "inspect_file_format": {},
    "extract_detected_metadata": {},
    "classify_observed_file": {},
    "account_file_failures": {},
    "persist_observed_inventory": {"inventory_id": None, "expected_state_version": 0},
    "review_persisted_inventory": {"offset": 0, "limit": 100},
}


@dataclass(frozen=True)
class DelegationPolicy:
    endpoints: dict[str, str]
    concurrency: int = 4
    allow_inventory_writes: bool = False

    def __post_init__(self):
        if not self.endpoints or set(self.endpoints) - AGENT_OPERATIONS.keys():
            raise ValueError("Configure only named specialist endpoints")
        if type(self.concurrency) is not int or not 1 <= self.concurrency <= 16:
            raise ValueError("Swarm concurrency must be between 1 and 16")
        for value in self.endpoints.values():
            https_url(value)


def prepare_task(task: dict, policy: DelegationPolicy) -> dict:
    if not isinstance(task, dict) or set(task) - {"agent", "operation", "arguments", "request_id"}:
        raise ValueError("A delegation needs an agent, operation, and arguments")
    agent, operation = task.get("agent"), task.get("operation")
    if agent not in policy.endpoints or operation not in AGENT_OPERATIONS.get(agent, ()):
        raise ValueError("The specialist or capability is not allowed")
    if agent == "persist-inventory" and not policy.allow_inventory_writes:
        raise ValueError("Inventory persistence requires explicit caller/operator authorization")
    arguments = task.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError("Operation arguments must be an object")
    required = {
        "inspect_file_format": {"file_id"},
        "extract_detected_metadata": {"file_id"},
        "classify_observed_file": {"file_id"},
        "account_file_failures": {"file_ids"},
        "persist_observed_inventory": {"file_ids"},
        "review_persisted_inventory": {"inventory_id"},
    }.get(operation, set())
    if not required <= arguments.keys() or set(arguments) - (
        required | DEFAULT_ARGUMENTS[operation].keys()
    ):
        raise ValueError("Missing or unknown operation arguments")
    arguments = DEFAULT_ARGUMENTS[operation] | arguments
    for key in ("max_files", "expected_state_version", "offset", "limit"):
        if key in arguments and (
            type(arguments[key]) is not int
            or arguments[key] < (0 if key in {"offset", "expected_state_version"} else 1)
        ):
            raise ValueError("Operation bounds and versions must be non-boolean integers")
    for key in ("file_id", "inventory_id"):
        if arguments.get(key) is not None:
            UUID(arguments[key])
    if "file_ids" in arguments:
        file_ids = arguments["file_ids"]
        if not isinstance(file_ids, list) or not file_ids:
            raise ValueError("Select at least one observed file ID")
        for file_id in file_ids:
            UUID(file_id)
    return {
        "agent": agent,
        "operation": operation,
        "arguments": arguments,
        "request_id": str(UUID(task["request_id"])) if task.get("request_id") else str(uuid4()),
    }


def check_receipt(receipt: dict, task: dict) -> dict:
    if (
        not isinstance(receipt, dict)
        or any(receipt.get(key) != task[key] for key in ("request_id", "operation", "arguments"))
        or receipt.get("status") not in {"succeeded", "failed"}
    ):
        raise RuntimeError("No matching terminal MCP receipt; the operation outcome is unknown")
    if json.dumps(receipt["arguments"], sort_keys=True) != json.dumps(
        task["arguments"], sort_keys=True
    ):
        raise RuntimeError("The receipt changed the operation argument types")
    return receipt


class Delegator:
    def __init__(
        self, settings, credential, policy: DelegationPolicy, invoke=None, collect_receipts=False
    ):
        self.settings = settings
        self.credential = credential
        self.policy = policy
        self.invoke = invoke or self._invoke_foundry
        self.batch_lock = asyncio.Lock()
        self.collect_receipts = collect_receipts
        self.receipts: list[dict] = []
        self.outcomes: list[dict] = []

    async def delegate(self, tasks: list[dict]) -> list[dict]:
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 32:
            raise ValueError("Delegate between 1 and 32 tasks per batch")
        prepared = [prepare_task(task, self.policy) for task in tasks]
        ids = [task["request_id"] for task in prepared]
        files = [
            task["arguments"]["file_id"] for task in prepared if "file_id" in task["arguments"]
        ]
        if len(set(ids)) != len(ids) or len(set(files)) != len(files):
            raise ValueError("A batch must contain unique requests and independent file tasks")
        if len(prepared) > 1 and any(
            task["operation"]
            in {
                "register_source_workspace",
                "discover_source_files",
                "persist_observed_inventory",
                "account_file_failures",
            }
            for task in prepared
        ):
            raise ValueError("Workspace, discovery, accounting, and commits use separate barriers")
        semaphore = asyncio.Semaphore(self.policy.concurrency)

        async def one(task):
            async with semaphore:
                try:
                    receipt = check_receipt(await self.invoke(task), task)
                    if self.collect_receipts:
                        self.receipts.append(receipt)
                    return {"agent": task["agent"], "receipt": receipt}
                except Exception:
                    return {
                        "agent": task["agent"],
                        "request_id": task["request_id"],
                        "operation": task["operation"],
                        "status": "outcome_unknown",
                        "error": "No authoritative terminal receipt; investigate before retrying.",
                    }

        async with self.batch_lock:
            results = await asyncio.gather(*(one(task) for task in prepared))
            if self.collect_receipts:
                self.outcomes.extend(results)
            return results

    async def _invoke_foundry(self, task: dict) -> dict:
        import httpx

        token = await asyncio.to_thread(
            self.credential.get_token,
            "https://ai.azure.com/.default",
        )
        async with httpx.AsyncClient(timeout=1800, follow_redirects=False) as client:
            # A failed response may still follow a successful side effect. Never retry it blindly.
            try:
                response = await client.post(
                    self.policy.endpoints[task["agent"]],
                    headers={"Authorization": f"Bearer {token.token}"},
                    json={
                        "input": (
                            "Execute exactly one requested capability using this JSON task. "
                            "Copy request_id and arguments exactly; call no extra operations. "
                            "Return the unchanged MCP receipt.\n" + json.dumps(task)
                        ),
                        "stream": False,
                    },
                )
                response.raise_for_status()
            except httpx.HTTPError:
                pass
            token = await asyncio.to_thread(self.credential.get_token, self.settings.backend_scope)
            response = await client.get(
                f"{self.settings.backend_url}/operations/{task['request_id']}",
                headers={"Authorization": f"Bearer {token.token}"},
            )
            response.raise_for_status()
            return response.json()
