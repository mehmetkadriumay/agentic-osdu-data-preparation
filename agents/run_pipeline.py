"""Invoke seven independent Responses endpoints, verifying backend state after each."""

import argparse
import json
from pathlib import Path
from uuid import UUID, uuid4

from osdu_foundry.settings import https_url
from osdu_foundry.stages import STAGES


def checked_receipt(receipt: dict, *, run_id: str, inventory_id: str, stage: int) -> None:
    if (
        receipt.get("run_id") != run_id
        or receipt.get("inventory_id") != inventory_id
        or type(receipt.get("completed_stage")) is not int
        or receipt["completed_stage"] < stage
        or receipt.get("status") == "blocked"
    ):
        raise RuntimeError("The backend did not confirm the requested stage; refusing to continue")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoints", required=True, type=Path)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--backend-scope", required=True)
    parser.add_argument("--max-files", type=int, default=100, help="New-run discovery budget")
    parser.add_argument("--run-id", type=UUID)
    parser.add_argument("--inventory-id", type=UUID)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--execute", action="store_true", help="Permit model calls and inventory writes"
    )
    args = parser.parse_args()
    endpoints = json.loads(args.endpoints.read_text(encoding="utf-8-sig"))
    if set(endpoints) != {stage.name for stage in STAGES}:
        parser.error("Provide exactly one full Responses endpoint for each of the seven stages")
    endpoints = {key: https_url(value) for key, value in endpoints.items()}
    backend = https_url(args.backend_url, root_only=True)
    if args.max_files < 1:
        parser.error("--max-files must be positive")
    if not args.backend_scope.endswith("/.default"):
        parser.error("--backend-scope must end with /.default")
    if args.run_id and not args.inventory_id:
        parser.error("--run-id continuation requires the original --inventory-id")
    if args.output.exists():
        parser.error("--output must not overwrite an existing report")
    if not args.execute:
        print(
            "Plan only: invoke seven agents in order, verify receipts, write one local JSON report."
        )
        return

    import httpx
    from azure.identity import DefaultAzureCredential

    run_id = str(args.run_id or uuid4())
    inventory_id = str(args.inventory_id or uuid4())
    print(f"run_id={run_id} inventory_id={inventory_id}", flush=True)
    with (
        DefaultAzureCredential() as credential,
        httpx.Client(timeout=1800, follow_redirects=False) as client,
    ):

        def backend_get(path: str) -> dict:
            token = credential.get_token(args.backend_scope).token
            response = client.get(f"{backend}{path}", headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            return response.json()

        prior = backend_get(f"/runs/{run_id}") if args.run_id else None
        version = (
            prior["request"]["expected_state_version"]
            if prior
            else backend_get(f"/inventories/{inventory_id}")["state_version"]
        )
        payload = (
            prior["request"]
            if prior
            else {
                "run_id": run_id,
                "inventory_id": inventory_id,
                "expected_state_version": version,
                "max_files": args.max_files,
            }
        )
        if payload["run_id"] != run_id or payload["inventory_id"] != inventory_id:
            raise RuntimeError("The continuation IDs do not match the backend run")
        for stage in STAGES:
            if prior and prior["completed_stage"] >= stage.number:
                checked_receipt(prior, run_id=run_id, inventory_id=inventory_id, stage=stage.number)
                continue
            token = credential.get_token("https://ai.azure.com/.default").token
            response = client.post(
                endpoints[stage.name],
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "input": (
                        f"Execute only stage {stage.number} using exactly this run configuration: "
                        + json.dumps(payload)
                        + ". Call run_stage and return its receipt. Do not change any values."
                    ),
                    "stream": False,
                },
            )
            response.raise_for_status()
            receipt = backend_get(f"/runs/{run_id}")
            checked_receipt(receipt, run_id=run_id, inventory_id=inventory_id, stage=stage.number)
            print(f"{stage.name}: {receipt['status']}", flush=True)
        report = {"run": backend_get(f"/runs/{run_id}"), "items": []}
        offset = 0
        while True:
            page = backend_get(f"/runs/{run_id}/review?offset={offset}&limit=100")
            report["items"].extend(page["items"])
            offset += len(page["items"])
            if offset >= page["total_count"]:
                break
            if not page["items"]:
                raise RuntimeError("The backend returned an incomplete review page")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
