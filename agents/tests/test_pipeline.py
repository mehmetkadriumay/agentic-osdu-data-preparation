import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from osdu_foundry.settings import https_url
from osdu_foundry.stages import STAGES
from run_pipeline import checked_receipt


class PipelineTests(unittest.TestCase):
    def test_cli_defaults_to_offline_plan_without_writing_a_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            endpoints = root / "endpoints.json"
            endpoints.write_text(
                json.dumps(
                    {
                        stage.name: f"https://agents.example.com/{stage.name}/responses?api-version=v1"
                        for stage in STAGES
                    }
                )
            )
            output = root / "report.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "run_pipeline.py"),
                    "--endpoints",
                    str(endpoints),
                    "--backend-url",
                    "https://backend.example.com",
                    "--backend-scope",
                    "api://test/.default",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            assert "Plan only" in result.stdout
            assert not output.exists()

    def test_model_claims_cannot_substitute_for_backend_confirmation(self):
        receipt = {
            "run_id": "run",
            "inventory_id": "inventory",
            "completed_stage": 6,
            "status": "needs_review",
        }
        checked_receipt(receipt, run_id="run", inventory_id="inventory", stage=6)
        for change in ({"completed_stage": 5}, {"run_id": "invented"}, {"status": "blocked"}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                checked_receipt(receipt | change, run_id="run", inventory_id="inventory", stage=6)

    def test_endpoints_must_use_https_without_embedded_credentials(self):
        for value in (
            "http://backend.example.com",
            "https://user:secret@backend.example.com",
            "https://backend.example.com/#fragment",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                https_url(value)
        assert https_url("https://backend.example.com/") == "https://backend.example.com"


@unittest.skipUnless(
    importlib.util.find_spec("sqlalchemy") and importlib.util.find_spec("pydantic"),
    "Install the root project to run real-tool integration",
)
class RealToolWorkflowTests(unittest.TestCase):
    def test_registered_tools_persist_metadata_and_unsupported_files_without_source_writes(self):
        import tempfile
        from uuid import uuid4

        from osdu_cloud.gateway import ToolGateway
        from osdu_cloud.ledger import SqlLedger
        from osdu_cloud.workflow import RunRequest, Workflow

        from agentic_osdu.runtime import create_runtime

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "source" / "Data"
            data.mkdir(parents=True)
            csv = data / "coordinates.csv"
            csv.write_bytes(b"X,Y,Z\n1,2,3\n4,5,6\n")
            unknown = data / "unknown.bin"
            unknown.write_bytes(b"\x00\x01\xff\xfe")
            original = {file: file.read_bytes() for file in data.iterdir()}
            runtime = create_runtime(root / "state.db")
            try:
                workflow = Workflow(
                    ToolGateway(runtime), SqlLedger(runtime.database), str(data.parent), "Data"
                )
                request = RunRequest(str(uuid4()), str(uuid4()))
                for stage in range(1, 8):
                    result = workflow.execute(stage, request, str(uuid4()))
                assert result["completed_stage"] == 7
                assert result["state_version"] == 1
                assert result["counts"]["files"] == 2
                state = workflow.ledger.get(request.run_id)
                csv_item = next(
                    item
                    for item in state["review_items"]
                    if item["file"]["relative_path"].endswith("coordinates.csv")
                )
                assert csv_item["extracted_metadata"]
                assert csv_item["detected_formats"][0] == "CSV"
                metadata = csv_item["extracted_metadata"][0]
                assert (
                    metadata["reference"]["extraction_id"]
                    in csv_item["classification"]["extraction_ids"]
                )
                assert any(
                    failure["code"] == "UNSUPPORTED_FORMAT" for failure in result["failures"]
                )
                assert {file: file.read_bytes() for file in data.iterdir()} == original
            finally:
                runtime.database.dispose()


if __name__ == "__main__":
    unittest.main()
