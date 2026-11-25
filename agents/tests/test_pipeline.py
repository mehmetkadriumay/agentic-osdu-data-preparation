import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))

from osdu_agent_host.delegation import (
    DelegationPolicy,
    Delegator,
    check_receipt,
    prepare_task,
)
from osdu_agent_host.settings import https_url

ROOT = Path(__file__).resolve().parents[2]


class DelegationTests(unittest.IsolatedAsyncioTestCase):
    def policy(self, **values):
        return DelegationPolicy(
            {
                "extract-metadata": "https://agents.example.com/extract/responses",
                "classify-data": "https://agents.example.com/classify/responses",
                "persist-inventory": "https://agents.example.com/persist/responses",
            },
            **values,
        )

    async def test_parallel_delegation_is_bounded_and_uses_real_receipts(self):
        active = peak = 0
        started = asyncio.Event()

        async def invoke(task):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                started.set()
            await asyncio.wait_for(started.wait(), timeout=2)
            await asyncio.sleep(0)
            active -= 1
            return {key: task[key] for key in ("request_id", "operation", "arguments")} | {
                "status": "succeeded",
                "result": {"authoritative": True},
            }

        delegator = Delegator(None, None, self.policy(concurrency=2), invoke=invoke)
        results = await delegator.delegate(
            [
                {
                    "agent": "extract-metadata",
                    "operation": "extract_detected_metadata",
                    "arguments": {"file_id": str(uuid4())},
                }
                for _ in range(3)
            ]
        )
        assert peak == 2
        assert all(result["receipt"]["result"]["authoritative"] for result in results)

    async def test_failure_or_invented_success_does_not_hide_other_files(self):
        async def invoke(task):
            if task["arguments"]["file_id"] == first:
                return {"status": "succeeded", "request_id": "invented"}
            return {key: task[key] for key in ("request_id", "operation", "arguments")} | {
                "status": "failed",
                "error": {"code": "UNSUPPORTED_FORMAT"},
            }

        first = str(uuid4())
        delegator = Delegator(None, None, self.policy(), invoke=invoke)
        results = await delegator.delegate(
            [
                {
                    "agent": "extract-metadata",
                    "operation": "extract_detected_metadata",
                    "arguments": {"file_id": file_id},
                }
                for file_id in (first, str(uuid4()))
            ]
        )
        assert results[0]["status"] == "outcome_unknown"
        assert results[1]["receipt"]["error"]["code"] == "UNSUPPORTED_FORMAT"

    async def test_dependent_same_file_tasks_and_mixed_commits_are_rejected_before_calls(self):
        calls = []

        async def invoke(task):
            calls.append(task)

        delegator = Delegator(None, None, self.policy(allow_inventory_writes=True), invoke=invoke)
        file_id = str(uuid4())
        extraction = {
            "agent": "extract-metadata",
            "operation": "extract_detected_metadata",
            "arguments": {"file_id": file_id},
        }
        classification = {
            "agent": "classify-data",
            "operation": "classify_observed_file",
            "arguments": {"file_id": file_id},
        }
        commit = {
            "agent": "persist-inventory",
            "operation": "persist_observed_inventory",
            "arguments": {"file_ids": [file_id]},
        }
        for batch in ([extraction, classification], [extraction, commit]):
            with self.assertRaises(ValueError):
                await delegator.delegate(batch)
        assert calls == []

    async def test_model_arguments_cannot_authorize_persistence_or_add_endpoints(self):
        for task in (
            {
                "agent": "persist-inventory",
                "operation": "persist_observed_inventory",
                "arguments": {"file_ids": [], "allow_inventory_writes": True},
            },
            {"agent": "evil", "operation": "extract_detected_metadata", "arguments": {}},
            {
                "agent": "extract-metadata",
                "operation": "register_source_workspace",
                "arguments": {},
            },
        ):
            with self.subTest(task=task), self.assertRaises(ValueError):
                prepare_task(task, self.policy())


class InvocationTests(unittest.TestCase):
    def test_independent_invocation_and_swarm_default_to_offline_plans(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = root / "arguments.json"
            arguments.write_text(json.dumps({"file_id": str(uuid4())}))
            endpoints = root / "endpoints.json"
            endpoints.write_text(
                json.dumps(
                    {
                        "extract-metadata": "https://agents.example.com/extract/responses",
                    }
                )
            )
            output = root / "report.json"
            common = [
                "--backend-url",
                "https://backend.example.com",
                "--backend-scope",
                "api://test/.default",
                "--output",
                str(output),
            ]
            commands = [
                [
                    str(ROOT / "agents" / "invoke.py"),
                    "--agent",
                    "extract-metadata",
                    "--operation",
                    "extract_detected_metadata",
                    "--arguments",
                    str(arguments),
                    "--endpoint",
                    "https://agents.example.com/extract/responses",
                    *common,
                ],
                [
                    str(ROOT / "swarms" / "exploration" / "run.py"),
                    "--endpoints",
                    str(endpoints),
                    "--project-endpoint",
                    "https://project.example.com",
                    "--model",
                    "model",
                    "--request",
                    "Extract one observed file",
                    *common,
                ],
            ]
            for command in commands:
                result = subprocess.run(
                    [sys.executable, *command],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                assert result.returncode == 0, result.stderr
                assert "Plan only" in result.stdout
                assert not output.exists()

    def test_endpoint_validation_and_receipt_matching(self):
        for value in (
            "http://server.example.com",
            "https://user:secret@server.example.com",
            "https://server.example.com/#fragment",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                https_url(value)
        task = {"request_id": str(uuid4()), "operation": "inspect_file_format", "arguments": {}}
        with self.assertRaises(RuntimeError):
            check_receipt(task | {"status": "in_progress"}, task)
        versioned = task | {"arguments": {"expected_state_version": 1}}
        with self.assertRaises(RuntimeError):
            check_receipt(
                versioned | {"status": "succeeded", "arguments": {"expected_state_version": True}},
                versioned,
            )


@unittest.skipUnless(
    importlib.util.find_spec("sqlalchemy")
    and importlib.util.find_spec("pydantic")
    and importlib.util.find_spec("mcp"),
    "Real MCP/core integration requires the root dependencies",
)
class RealMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_named_servers_share_authoritative_observations_and_persist_failure_evidence(
        self,
    ):
        from types import SimpleNamespace

        from mcp import Client
        from mcp_servers.classification.server import create_server as classifier
        from mcp_servers.extraction.server import create_server as extractor
        from mcp_servers.inventory_review.server import create_server as inventory
        from mcp_servers.shared.gateway import ToolGateway
        from mcp_servers.shared.ledger import SqlLedger
        from mcp_servers.shared.service import OperationService
        from mcp_servers.shared.transport import caller_context
        from mcp_servers.workspace_discovery.server import create_server as discovery

        from agentic_osdu.runtime import create_runtime

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "source" / "Data"
            data.mkdir(parents=True)
            source = data / "coordinates.csv"
            source.write_bytes(b"X,Y,Z\n1,2,3\n4,5,6\n")
            original = source.read_bytes()
            runtime = create_runtime(root / "state.db")
            try:
                service = OperationService(
                    ToolGateway(runtime), SqlLedger(runtime.database), str(data.parent)
                )

                async def call(factory, role, operation, **arguments):
                    token = caller_context.set(SimpleNamespace(principal_id=f"p-{role}", role=role))
                    try:
                        async with Client(factory(service)) as client:
                            result = await client.call_tool(
                                operation,
                                {"request_id": str(uuid4()), **arguments},
                            )
                            assert not result.is_error, result.content
                            receipt = result.structured_content
                            assert receipt["status"] == "succeeded", receipt
                            return receipt["result"]
                    finally:
                        caller_context.reset(token)

                await call(discovery, 1, "register_source_workspace")
                observed = await call(discovery, 2, "discover_source_files")
                file_id = observed["output"]["files"][0]["file_id"]
                await call(discovery, 2, "inspect_file_format", file_id=file_id)
                await call(extractor, 3, "extract_detected_metadata", file_id=file_id)
                await call(classifier, 4, "classify_observed_file", file_id=file_id)
                committed = await call(
                    inventory, 6, "persist_observed_inventory", file_ids=[file_id]
                )
                reviewed = await call(
                    inventory,
                    7,
                    "review_persisted_inventory",
                    inventory_id=committed["inventory_id"],
                )
                item = reviewed["output"]["items"][0]
                assert item["extracted_metadata"]
                reference = item["extracted_metadata"][0]["reference"]["extraction_id"]
                assert reference in item["classification"]["extraction_ids"]
                assert source.read_bytes() == original
            finally:
                runtime.database.dispose()


if __name__ == "__main__":
    unittest.main()
