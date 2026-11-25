"""Invoke one independent specialist; default to an offline, non-writing plan."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent / "shared"))

from osdu_agent_host.delegation import DelegationPolicy, Delegator, prepare_task
from osdu_agent_host.settings import https_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--arguments", required=True, type=Path, help="JSON operation arguments")
    parser.add_argument("--endpoint", required=True, help="Actual full Foundry Responses endpoint")
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--backend-scope", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-inventory-writes", action="store_true")
    args = parser.parse_args()
    settings = SimpleNamespace(
        backend_url=https_url(args.backend_url, root_only=True),
        backend_scope=args.backend_scope,
    )
    if not args.backend_scope.endswith("/.default") or args.output.exists():
        parser.error("Use an explicit /.default scope and a new output path")
    policy = DelegationPolicy(
        {args.agent: https_url(args.endpoint)},
        allow_inventory_writes=args.allow_inventory_writes,
    )
    task = prepare_task(
        {
            "agent": args.agent,
            "operation": args.operation,
            "arguments": json.loads(args.arguments.read_text(encoding="utf-8-sig")),
        },
        policy,
    )
    if not args.execute:
        print("Plan only: invoke one specialist and verify its MCP operation receipt.")
        return

    from azure.identity import DefaultAzureCredential

    async def execute(credential):
        delegator = Delegator(settings, credential, policy)
        return (await delegator.delegate([task]))[0]

    with DefaultAzureCredential() as credential:
        result = asyncio.run(execute(credential))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2)
        output.write("\n")
    if result.get("status") == "outcome_unknown" or result["receipt"]["status"] != "succeeded":
        raise SystemExit("The requested operation did not have a confirmed successful outcome.")


if __name__ == "__main__":
    main()
