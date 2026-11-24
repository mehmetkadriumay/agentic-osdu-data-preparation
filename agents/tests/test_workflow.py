import copy
import sys
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from osdu_cloud.workflow import RunRequest, Workflow, WorkflowError
from osdu_foundry.stages import STAGES, get_stage


class MemoryLedger:
    def __init__(self):
        self.rows = {}

    def get(self, run_id):
        return copy.deepcopy(self.rows.get(run_id))

    def save(self, state):
        self.rows[state["request"]["run_id"]] = copy.deepcopy(state)

    def has_active(self, instance_id):
        return any(
            row["instance_id"] == instance_id
            and row["completed_stage"] < 6
            and row["status"] != "blocked"
            for row in self.rows.values()
        )


class FakeGateway:
    def __init__(self):
        self.file_ids = [str(uuid4()), str(uuid4())]
        self.calls = []
        self.fail_extract = set()
        self.extension_only = False
        self.ambiguous = False
        self.review_size = 1

    def call(self, tool_id, request_id, workspace_id, actor, value):
        self.calls.append((tool_id, value, request_id, actor))
        evidence = []
        if tool_id == "TOOL-001":
            output = {
                "workspace_id": str(uuid4()),
                "canonical_root": "/mnt/source",
                "policy_fingerprint": "a" * 64,
                "read_only": True,
            }
        elif tool_id == "TOOL-002":
            output = {
                "files": [
                    {"file_id": file_id, "relative_path": f"Data/{index}.csv", "size_bytes": 100}
                    for index, file_id in enumerate(self.file_ids)
                ]
            }
        elif tool_id == "TOOL-003":
            output = {"sample": {"sample_id": str(uuid4())}, "content_base64": "untrusted"}
        elif tool_id == "TOOL-004":
            candidates = [{"format_id": "CSV", "confidence": 0.9, "evidence_ids": ["proof"]}]
            if self.ambiguous:
                candidates.append({"format_id": "TEXT", "confidence": 0.89, "evidence_ids": []})
            evidence = [
                {
                    "evidence_id": "proof",
                    "rule_id": "FORMAT-1.EXT-CSV" if self.extension_only else "FORMAT-1.SIG-CSV",
                }
            ]
            output = {"detection": {"file_id": value["file_id"], "candidates": candidates}}
        elif tool_id == "TOOL-011":
            if value["file_id"] in self.fail_extract:
                raise WorkflowError("EXTRACTION_FAILED")
            output = {"columns": ["X", "Y"], "row_count": 1}
        elif tool_id == "TOOL-005":
            output = {
                "classification": {"file_id": value["file_id"], "extraction_ids": ["real-ref"]}
            }
        elif tool_id == "TOOL-022":
            output = {"state_version": value["expected_state_version"] + 1}
        elif tool_id == "TOOL-027":
            offset = value["query"]["offset"]
            ids = self.file_ids[offset : offset + self.review_size]
            output = {
                "state_version": 1,
                "total_count": len(self.file_ids),
                "items": [{"file": {"file_id": file_id}} for file_id in ids],
            }
        else:
            raise AssertionError(tool_id)
        return {"status": "succeeded", "output": output, "evidence": evidence}

    def extraction(self, format_id, file_id):
        return "TOOL-011", {"file_id": file_id}

    def classification(self, file_id):
        return {"file_id": file_id, "extracted_metadata": ["authoritative-reference"]}

    def snapshot(self, state):
        return {"files": list(state["files"]), "extracted_metadata": ["authoritative-metadata"]}

    def review_records(self, inventory_id, file_ids):
        return {
            file_id: {
                "detection": {"file_id": file_id},
                "detected_formats": ["CSV"],
                "extracted_metadata": [{"reference": "real-ref", "payload": {"row_count": 1}}],
            }
            for file_id in file_ids
        }


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.ledger = MemoryLedger()
        self.workflow = Workflow(self.gateway, self.ledger, "/mnt/source", "Data")
        self.request = RunRequest(str(uuid4()), str(uuid4()), max_files=10)

    def advance(self, through=7):
        result = None
        for stage in range(1, through + 1):
            result = self.workflow.execute(stage, self.request, f"principal-{stage}")
        return result

    def test_pipeline_keeps_failures_and_real_metadata_and_paginates(self):
        self.gateway.fail_extract.add(self.gateway.file_ids[0])
        result = self.advance()
        assert result["completed_stage"] == 7
        assert result["status"] == "needs_review"
        assert result["counts"]["files"] == 2
        assert result["counts"]["extracted"] == 1
        assert result["failures"][0]["code"] == "EXTRACTION_FAILED"
        assert len([call for call in self.gateway.calls if call[0] == "TOOL-027"]) == 2
        state = self.ledger.get(self.request.run_id)
        assert state["inventory_mutation"]["extracted_metadata"] == ["authoritative-metadata"]
        assert state["review_items"][1]["extracted_metadata"][0]["reference"] == "real-ref"
        assert "untrusted" not in repr(state)
        registration = self.gateway.calls[0][1]
        assert registration["read_only"]
        assert registration["allowed_output_subpaths"] == []
        discovery = next(call[1] for call in self.gateway.calls if call[0] == "TOOL-002")
        assert discovery["include_paths"] == ["Data"]
        assert not discovery["follow_symlinks"]

    def test_extension_only_and_ambiguous_files_are_not_extracted(self):
        for kind in ("extension_only", "ambiguous"):
            with self.subTest(kind=kind):
                self.setUp()
                setattr(self.gateway, kind, True)
                result = self.advance()
                assert result["counts"]["extracted"] == 0
                assert result["counts"]["classified"] == 0
                assert result["counts"]["files"] == 2
                assert not any(call[0] == "TOOL-011" for call in self.gateway.calls)

    def test_order_and_configuration_are_enforced_and_stages_are_idempotent(self):
        with self.assertRaisesRegex(WorkflowError, "WORKSPACE_STAGE_REQUIRED"):
            self.workflow.execute(2, self.request, "actor")
        self.advance(1)
        calls = len(self.gateway.calls)
        self.workflow.execute(1, self.request, "actor")
        assert len(self.gateway.calls) == calls
        with self.assertRaisesRegex(WorkflowError, "PRECEDING_STAGE_REQUIRED"):
            self.workflow.execute(3, self.request, "actor")
        altered = RunRequest(self.request.run_id, self.request.inventory_id, max_files=11)
        with self.assertRaisesRegex(WorkflowError, "RUN_CONFIGURATION_CONFLICT"):
            self.workflow.execute(2, altered, "actor")
        with self.assertRaisesRegex(WorkflowError, "ANOTHER_RUN_IS_ACTIVE"):
            self.workflow.execute(1, RunRequest(str(uuid4()), str(uuid4())), "actor")

    def test_backend_restart_blocks_unpersisted_work_but_allows_review(self):
        self.advance(1)
        restarted = Workflow(self.gateway, self.ledger, "/mnt/source", "Data")
        with self.assertRaisesRegex(WorkflowError, "BACKEND_RESTART_REQUIRES_NEW_RUN"):
            restarted.execute(2, self.request, "actor")
        self.advance(6)
        assert restarted.execute(7, self.request, "actor")["completed_stage"] == 7

    def test_stage_capabilities_do_not_include_manifest_or_ingestion_tools(self):
        capabilities = set().union(*(stage.tool_ids for stage in STAGES))
        assert not capabilities.intersection({f"TOOL-{number:03}" for number in range(14, 22)})
        assert not capabilities.intersection({"TOOL-029", "TOOL-030"})
        with self.assertRaises(ValueError):
            get_stage(True)

    def test_request_rejects_scope_injection_and_invalid_budgets(self):
        for changes in (
            {"root_path": "C:\\other"},
            {"max_files": True},
            {"max_files": 101},
            {"expected_state_version": -1},
        ):
            value = {"run_id": self.request.run_id, "inventory_id": self.request.inventory_id}
            value.update(changes)
            with self.subTest(changes=changes), self.assertRaises(WorkflowError):
                RunRequest.from_dict(value, 100)


if __name__ == "__main__":
    unittest.main()
