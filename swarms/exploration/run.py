"""Run a local Agent Framework supervisor over independently hosted specialists."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents" / "shared"))

from osdu_agent_host.delegation import DelegationPolicy, Delegator
from osdu_agent_host.settings import Settings, https_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoints", required=True, type=Path)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--backend-scope", required=True)
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--request", required=True, help="The supervisor's caller-authorized task")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-inventory-writes", action="store_true")
    args = parser.parse_args()
    policy = DelegationPolicy(
        json.loads(args.endpoints.read_text(encoding="utf-8-sig")),
        args.concurrency,
        args.allow_inventory_writes,
    )
    settings = Settings(
        https_url(args.project_endpoint),
        args.model,
        https_url(args.backend_url, root_only=True),
        args.backend_scope,
    )
    if not args.backend_scope.endswith("/.default") or args.output.exists() or args.timeout < 1:
        parser.error("Use an explicit /.default scope, a new output path, and a positive timeout")
    if not args.execute:
        print(
            f"Plan only: supervisor-led swarm; concurrency={policy.concurrency}; "
            f"inventory_writes={policy.allow_inventory_writes}. No model calls or output writes."
        )
        return

    from agent import create_agent
    from azure.identity import DefaultAzureCredential

    async def execute(credential):
        delegator = Delegator(settings, credential, policy, collect_receipts=True)
        agent = create_agent(settings, credential, policy, delegator)
        report = {"receipts": delegator.receipts, "delegations": delegator.outcomes}
        try:
            async with asyncio.timeout(args.timeout):
                response = await agent.run(args.request)
            report.update(supervisor_summary=str(response), status="returned")
        except Exception:
            report.update(
                status="blocked",
                error="Supervisor execution failed or timed out. Inspect receipts before retrying.",
            )
        return report

    with DefaultAzureCredential() as credential:
        report = asyncio.run(execute(credential))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    if report["status"] == "blocked" or any(
        result.get("status") == "outcome_unknown" for result in report["delegations"]
    ):
        raise SystemExit("The swarm has incomplete or unknown outcomes; inspect the saved report.")


if __name__ == "__main__":
    main()
