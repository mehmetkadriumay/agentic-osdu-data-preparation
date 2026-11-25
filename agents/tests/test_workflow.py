import asyncio
import copy
import importlib.util
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp_servers.shared.service import OperationError, OperationService


class MemoryLedger:
    def __init__(self):
        self.rows = {}

    def get(self, request_id):
        return copy.deepcopy(self.rows.get(request_id))

    def save(self, receipt):
        self.rows[receipt["request_id"]] = copy.deepcopy(receipt)

    def inventory_receipt(self, inventory_id):
        rows = [
            row
            for row in self.rows.values()
            if row["operation"] == "persist_observed_inventory"
            and row["status"] == "succeeded"
            and row["result"]["inventory_id"] == inventory_id
        ]
        return (
            copy.deepcopy(max(rows, key=lambda row: row["result"]["output"]["state_version"]))
            if rows
            else None
        )


class FakeGateway:
    def __init__(self):
        self.file_ids = [str(uuid4()), str(uuid4())]
        self.calls = []
        self.fail_extract = set()
        self.extension_only = False
        self.ambiguous = False
        self.extraction_hook = lambda: None
        self.committed = threading.Event()

    def call(self, tool_id, request_id, workspace_id, actor, value):
        self.calls.append((tool_id, value))
        evidence = []
        if tool_id == "TOOL-001":
            output = {
                "workspace_id": str(uuid4()),
                "canonical_root": "/mnt/source",
                "read_only": True,
                "policy_fingerprint": "a" * 64,
            }
        elif tool_id == "TOOL-002":
            output = {
                "files": [
                    {"file_id": file_id, "relative_path": f"Data/{i}.csv"}
                    for i, file_id in enumerate(self.file_ids)
                ]
            }
        elif tool_id == "TOOL-003":
            output = {"sample": {"sample_id": str(uuid4())}, "content_base64": "untrusted"}
        elif tool_id == "TOOL-004":
            candidates = [{"format_id": "CSV", "confidence": 0.9, "evidence_ids": ["proof"]}]
            if self.ambiguous:
                candidates.append({"format_id": "TEXT", "confidence": 0.89})
            output = {"detection": {"file_id": value["file_id"], "candidates": candidates}}
            evidence = [
                {
                    "evidence_id": "proof",
                    "rule_id": "FORMAT.EXT-CSV" if self.extension_only else "FORMAT.SIG-CSV",
                }
            ]
        elif tool_id == "TOOL-011":
            self.extraction_hook()
            if value["file_id"] in self.fail_extract:
                raise OperationError("EXTRACTION_FAILED")
            output = {"row_count": 2}
        elif tool_id == "TOOL-005":
            output = {"classification": {"file_id": value["file_id"], "extraction_ids": ["real"]}}
        elif tool_id == "TOOL-022":
            self.committed.set()
            output = {"state_version": value["expected_state_version"] + 1}
        elif tool_id == "TOOL-027":
            ids = self.file_ids[value["query"]["offset"] :][: value["query"]["limit"]]
            output = {
                "items": [{"file": {"file_id": file_id}} for file_id in ids],
                "state_version": 1,
                "total_count": len(self.file_ids),
            }
        else:
            raise AssertionError(tool_id)
        return {"status": "succeeded", "output": output, "evidence": evidence}

    def extraction(self, format_id, file_id):
        return "TOOL-011", {"file_id": file_id}

    def classification(self, file_id):
        return {"file_id": file_id, "extracted_metadata": ["authoritative"]}

    def snapshot(self, inventory_id, files):
        return {
            "files": list(files),
            "extracted_metadata": ["authoritative"],
            "evidence": [
                {
                    "evidence_id": str(uuid4()),
                    "evidence_type": "operation_failure",
                    "source_file_id": file_id,
                    "observed_value": warning["code"],
                }
                for file_id, item in files.items()
                for warning in item["warnings"]
            ],
        }

    def review_records(self, inventory_id, file_ids):
        return {
            file_id: {"extracted_metadata": [{"reference": "real", "row_count": 2}]}
            for file_id in file_ids
        }


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.ledger = MemoryLedger()
        self.service = OperationService(self.gateway, self.ledger, "/mnt/source")

    def call(self, operation, role, **arguments):
        return self.service.execute(operation, str(uuid4()), arguments, f"principal-{role}", role)

    def discover(self):
        self.call("register_source_workspace", 1)
        self.call("discover_source_files", 2)
        for file_id in self.gateway.file_ids:
            self.call("inspect_file_format", 2, file_id=file_id)

    def test_capabilities_are_independent_not_ordered_global_stages(self):
        self.discover()
        file_id = self.gateway.file_ids[0]
        # Classification may explicitly operate on detection alone, without another file's work.
        result = self.call("classify_observed_file", 4, file_id=file_id)
        assert result["status"] == "succeeded"
        assert "completed_stage" not in result
        assert self.call("extract_detected_metadata", 3, file_id=file_id)["status"] == "succeeded"
        assert all(call[1].get("root_path") != "C:\\other" for call in self.gateway.calls)
        registration = self.gateway.calls[0][1]
        assert registration["read_only"]
        assert registration["allowed_output_subpaths"] == []
        discovery = next(value for tool, value in self.gateway.calls if tool == "TOOL-002")
        assert discovery["include_paths"] == ["Data"]
        assert not discovery["follow_symlinks"]
        assert "untrusted" not in repr(self.service.files)

    def test_different_files_extract_concurrently(self):
        self.discover()
        barrier = threading.Barrier(2)
        self.gateway.extraction_hook = lambda: barrier.wait(timeout=3)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self.call, "extract_detected_metadata", 3, file_id=file_id)
                for file_id in self.gateway.file_ids
            ]
            assert all(future.result(timeout=5)["status"] == "succeeded" for future in futures)

    def test_commit_waits_for_active_extraction_and_preserves_failures(self):
        self.discover()
        started, release = threading.Event(), threading.Event()

        def hold():
            started.set()
            if not release.wait(timeout=3):
                raise RuntimeError("Test extraction did not release")

        self.gateway.extraction_hook = hold
        with ThreadPoolExecutor(max_workers=2) as pool:
            extraction = pool.submit(
                self.call,
                "extract_detected_metadata",
                3,
                file_id=self.gateway.file_ids[0],
            )
            assert started.wait(timeout=2)
            commit = pool.submit(
                self.call, "persist_observed_inventory", 6, file_ids=self.gateway.file_ids
            )
            try:
                assert not self.gateway.committed.wait(timeout=0.05)
            finally:
                release.set()
            assert extraction.result(timeout=5)["status"] == "succeeded"
            receipt = commit.result(timeout=5)
        assert receipt["status"] == "succeeded"
        assert receipt["result"]["snapshot"]["extracted_metadata"] == ["authoritative"]
        assert receipt["result"]["snapshot"]["evidence"]

    def test_same_file_operations_do_not_overlap(self):
        self.discover()
        active = 0
        lock = threading.Lock()

        def assert_single():
            nonlocal active
            with lock:
                active += 1
                assert active == 1
            threading.Event().wait(0.03)
            with lock:
                active -= 1

        self.gateway.extraction_hook = assert_single
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    self.call, "extract_detected_metadata", 3, file_id=self.gateway.file_ids[0]
                )
                for _ in range(2)
            ]
            assert all(future.result(timeout=5)["status"] == "succeeded" for future in futures)

    def test_ambiguous_extension_only_and_failed_files_remain_in_inventory(self):
        for condition, code in (
            ("ambiguous", "AMBIGUOUS_FORMAT"),
            ("extension_only", "CONTENT_EVIDENCE_REQUIRED"),
        ):
            with self.subTest(condition=condition):
                self.setUp()
                setattr(self.gateway, condition, True)
                self.discover()
                failed = self.call("extract_detected_metadata", 3, file_id=self.gateway.file_ids[0])
                assert failed["error"]["code"] == code
                persisted = self.call(
                    "persist_observed_inventory", 6, file_ids=self.gateway.file_ids
                )
                assert persisted["status"] == "succeeded"
                assert len(persisted["result"]["snapshot"]["files"]) == 2
                assert not any(tool == "TOOL-011" for tool, _ in self.gateway.calls)

    def test_request_repeats_are_idempotent_and_identity_bound(self):
        request_id = str(uuid4())
        receipt = self.service.execute("register_source_workspace", request_id, {}, "p", 1)
        assert self.service.execute("register_source_workspace", request_id, {}, "p", 1) == receipt
        assert len(self.gateway.calls) == 1
        with self.assertRaisesRegex(OperationError, "REQUEST_CONFIGURATION_CONFLICT"):
            self.service.execute("register_source_workspace", request_id, {}, "other", 1)
        with self.assertRaisesRegex(OperationError, "CAPABILITY_DENIED"):
            self.service.execute("register_source_workspace", str(uuid4()), {}, "p", 3)
        self.ledger.rows[request_id]["status"] = "in_progress"
        with self.assertRaisesRegex(OperationError, "OPERATION_OUTCOME_UNKNOWN"):
            self.service.execute("register_source_workspace", request_id, {}, "p", 1)

    def test_restart_allows_independent_review_not_stale_file_capabilities(self):
        self.discover()
        persisted = self.call("persist_observed_inventory", 6, file_ids=self.gateway.file_ids)
        inventory_id = persisted["result"]["inventory_id"]
        self.service = OperationService(self.gateway, self.ledger, "/mnt/source")
        reviewed = self.call(
            "review_persisted_inventory", 7, inventory_id=inventory_id, offset=0, limit=1
        )
        assert reviewed["status"] == "succeeded"
        item = reviewed["result"]["output"]["items"][0]
        assert item["extracted_metadata"]
        assert item["warnings"]
        assert item["human_review_required"]
        stale = self.call("extract_detected_metadata", 3, file_id=self.gateway.file_ids[0])
        assert stale["error"]["code"] == "FILE_CAPABILITY_EXPIRED"

    def test_scope_and_version_injections_are_rejected(self):
        assert (
            self.call("register_source_workspace", 1, root_path="C:\\other")["error"]["code"]
            == "OPERATION_INPUT_INVALID"
        )
        self.discover()
        assert (
            self.call(
                "persist_observed_inventory",
                6,
                file_ids=self.gateway.file_ids,
                expected_state_version=True,
            )["error"]["code"]
            == "STATE_VERSION_INVALID"
        )

    def test_standalone_legacy_review_flags_missing_receipt_and_metadata(self):
        self.gateway.review_records = lambda _inventory, _files: {}
        reviewed = self.call("review_persisted_inventory", 7, inventory_id=str(uuid4()))
        assert reviewed["status"] == "succeeded"
        warnings = reviewed["result"]["output"]["items"][0]["warnings"]
        assert {warning["code"] for warning in warnings} == {
            "OPERATION_RECEIPT_UNAVAILABLE",
            "METADATA_UNAVAILABLE",
        }


class AsyncMCPFacadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_handlers_offload_work_without_losing_verified_identity(self):
        class StubServer:
            def __init__(self, **_kwargs):
                self.functions = {}

            def tool(self):
                def register(function):
                    self.functions[function.__name__] = function
                    return function

                return register

        root = Path(__file__).resolve().parents[2]
        gateway, ledger = FakeGateway(), MemoryLedger()
        service = OperationService(gateway, ledger, "/mnt/source")
        for operation, role, arguments in (
            ("register_source_workspace", 1, {}),
            ("discover_source_files", 2, {}),
            *(("inspect_file_format", 2, {"file_id": file_id}) for file_id in gateway.file_ids),
        ):
            receipt = service.execute(operation, str(uuid4()), arguments, "operator", role)
            assert receipt["status"] == "succeeded"
        rendezvous = threading.Barrier(2)
        gateway.extraction_hook = lambda: rendezvous.wait(timeout=2)

        def load(name, path):
            spec = importlib.util.spec_from_file_location(name, root / path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        sdk, sdk_server = ModuleType("mcp"), ModuleType("mcp.server")
        sdk_server.MCPServer = StubServer
        with patch.dict(sys.modules, {"mcp": sdk, "mcp.server": sdk_server}):
            transport = load(
                "mcp_servers.shared.test_transport", Path("mcp_servers") / "shared" / "transport.py"
            )
            with patch.dict(sys.modules, {"mcp_servers.shared.transport": transport}):
                factory = load(
                    "test_extraction_facade", Path("mcp_servers") / "extraction" / "server.py"
                )
                server = factory.create_server(service)
                token = transport.caller_context.set(
                    SimpleNamespace(principal_id="verified", role=3)
                )
                try:
                    results = await asyncio.gather(
                        *(
                            server.functions["extract_detected_metadata"](
                                request_id=str(uuid4()),
                                file_id=file_id,
                            )
                            for file_id in gateway.file_ids
                        )
                    )
                finally:
                    transport.caller_context.reset(token)
        assert all(result["status"] == "succeeded" for result in results)
        assert all(result["principal_id"] == "verified" for result in results)


if __name__ == "__main__":
    unittest.main()
