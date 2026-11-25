"""A genuine supervisor: the model selects specialist tasks, not numbered stages."""

import json
import os

from agent_framework import tool
from osdu_agent_host.application import build_agent
from osdu_agent_host.delegation import DelegationPolicy, Delegator


def create_agent(settings, credential, policy=None, delegator=None):
    if policy is None:
        allow = os.environ.get("OSDU_ALLOW_INVENTORY_WRITES", "false")
        if allow not in {"true", "false"}:
            raise ValueError("OSDU_ALLOW_INVENTORY_WRITES must be true or false")
        policy = DelegationPolicy(
            endpoints=json.loads(os.environ["OSDU_AGENT_ENDPOINTS_JSON"]),
            concurrency=int(os.environ.get("OSDU_SWARM_CONCURRENCY", "4")),
            allow_inventory_writes=allow == "true",
        )
    delegator = delegator or Delegator(settings, credential, policy)

    @tool
    async def delegate_tasks(tasks: list[dict]) -> list[dict]:
        """Delegate named agent/operation/arguments tasks; independent files run concurrently.

        Allowed agents are approve-workspace, discover-formats, extract-metadata,
        classify-data, account-failures, persist-inventory, and review-inventory.
        Each task has agent, operation, arguments, and optionally request_id for
        an explicitly requested repeat. Omit request_id for a new task; the
        coordinator allocates it. Never repeat an unknown-outcome write.
        """
        return await delegator.delegate(tasks)

    return build_agent("exploration-supervisor", __file__, settings, credential, [delegate_tasks])
