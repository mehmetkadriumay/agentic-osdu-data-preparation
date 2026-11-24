"""Deterministic seven-stage execution; models never assemble persistence payloads."""

import re
import threading
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import UUID, uuid4, uuid5

from osdu_foundry.stages import get_stage


class WorkflowError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RunRequest:
    run_id: str
    inventory_id: str
    expected_state_version: int = 0
    max_files: int = 100

    @classmethod
    def from_dict(cls, value: dict, max_files: int) -> "RunRequest":
        if not isinstance(value, dict) or set(value) - {
            "run_id",
            "inventory_id",
            "expected_state_version",
            "max_files",
        }:
            raise WorkflowError("RUN_REQUEST_INVALID")
        try:
            request = cls(
                run_id=str(UUID(value["run_id"])),
                inventory_id=str(UUID(value["inventory_id"])),
                expected_state_version=value.get("expected_state_version", 0),
                max_files=value.get("max_files", 100),
            )
        except (TypeError, ValueError, KeyError, AttributeError) as error:
            raise WorkflowError("RUN_REQUEST_INVALID") from error
        if (
            type(request.expected_state_version) is not int
            or request.expected_state_version < 0
            or type(request.max_files) is not int
            or not 1 <= request.max_files <= max_files
        ):
            raise WorkflowError("RUN_REQUEST_INVALID")
        return request


class Ledger(Protocol):
    def get(self, run_id: str) -> dict | None: ...
    def save(self, state: dict) -> None: ...
    def has_active(self, instance_id: str) -> bool: ...


class Gateway(Protocol):
    def call(
        self, tool_id: str, request_id: str, workspace_id: str, actor: str, value: dict
    ) -> dict: ...
    def extraction(self, format_id: str, file_id: str) -> tuple[str, dict] | None: ...
    def classification(self, file_id: str) -> dict: ...
    def snapshot(self, state: dict) -> dict: ...
    def review_records(self, inventory_id: str, file_ids: list[str]) -> dict: ...


def receipt(state: dict) -> dict:
    return {
        "run_id": state["request"]["run_id"],
        "inventory_id": state["request"]["inventory_id"],
        "request": state["request"],
        "completed_stage": state["completed_stage"],
        "status": state["status"],
        "workspace": state["workspace"],
        "state_version": state["state_version"],
        "counts": {
            "files": len(state["files"]),
            "extracted": sum("extracted_metadata" in value for value in state["files"].values()),
            "classified": sum("classification" in value for value in state["files"].values()),
            "failures": len(state["failures"]),
        },
        "failures": state["failures"],
        "review_summary": state.get("review_summary"),
    }


class Workflow:
    def __init__(
        self, gateway: Gateway, ledger: Ledger, source_root: str, scan_subpath: str
    ) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.source_root = source_root
        self.scan_subpath = scan_subpath
        self.instance_id = str(uuid4())
        self.lock = threading.RLock()

    def execute(self, stage: int, request: RunRequest, principal_id: str) -> dict:
        get_stage(stage)
        with self.lock:
            state = self.ledger.get(request.run_id)
            if state is None:
                if stage != 1:
                    raise WorkflowError("WORKSPACE_STAGE_REQUIRED")
                if self.ledger.has_active(self.instance_id):
                    raise WorkflowError("ANOTHER_RUN_IS_ACTIVE")
                state = {
                    "request": asdict(request),
                    "instance_id": self.instance_id,
                    "completed_stage": 0,
                    "status": "in_progress",
                    "workspace": None,
                    "files": {},
                    "failures": [],
                    "state_version": None,
                    "stage_principals": {},
                }
            if state["request"] != asdict(request):
                raise WorkflowError("RUN_CONFIGURATION_CONFLICT")
            if state["status"] == "blocked":
                raise WorkflowError("BLOCKED_RUN_REQUIRES_NEW_RUN")
            if state["completed_stage"] >= stage:
                return receipt(state)
            if state["completed_stage"] != stage - 1:
                raise WorkflowError("PRECEDING_STAGE_REQUIRED")
            if state["instance_id"] != self.instance_id and stage != 7:
                raise WorkflowError("BACKEND_RESTART_REQUIRES_NEW_RUN")
            state["stage_principals"][str(stage)] = principal_id
            self.ledger.save(state)
            try:
                getattr(self, f"_stage_{stage}")(state, principal_id)
            except Exception:
                state["status"] = "blocked"
                self.ledger.save(state)
                raise
            state["completed_stage"] = stage
            state["status"] = (
                "needs_review"
                if state["failures"]
                else ("completed" if stage == 7 else "in_progress")
            )
            self.ledger.save(state)
            return receipt(state)

    def _failure(self, state: dict, stage: int, file_id: str | None, code: str) -> None:
        code = code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else "TOOL_EXECUTION_FAILED"
        failure = {
            "stage": stage,
            "file_id": file_id,
            "code": code,
            "message": "The deterministic operation could not establish the requested result.",
        }
        if failure not in state["failures"]:
            state["failures"].append(failure)

    def _call(
        self,
        state: dict,
        stage: int,
        actor: str,
        tool_id: str,
        value: dict,
        *,
        file_id: str | None = None,
    ) -> dict | None:
        if tool_id not in get_stage(stage).tool_ids:
            raise WorkflowError("STAGE_CAPABILITY_DENIED")
        request_id = str(uuid5(UUID(state["request"]["run_id"]), f"{stage}:{tool_id}:{file_id}"))
        try:
            result = self.gateway.call(
                tool_id,
                request_id,
                state["workspace"]["workspace_id"] if state["workspace"] else str(UUID(int=0)),
                actor,
                value,
            )
            for error in result.get("errors", []):
                self._failure(state, stage, file_id, error["code"])
            if (
                result["status"] not in {"succeeded", "partially_succeeded"}
                or result.get("output") is None
            ):
                self._failure(state, stage, file_id, "TOOL_RESULT_UNAVAILABLE")
                return None
            return result
        except Exception as error:
            self._failure(
                state, stage, file_id, str(getattr(error, "code", "TOOL_EXECUTION_FAILED"))
            )
            return None

    def _stage_1(self, state: dict, actor: str) -> None:
        result = self._call(
            state,
            1,
            actor,
            "TOOL-001",
            {"root_path": self.source_root, "read_only": True, "allowed_output_subpaths": []},
        )
        if result is None:
            raise WorkflowError("WORKSPACE_REGISTRATION_FAILED")
        state["workspace"] = result["output"]

    def _stage_2(self, state: dict, actor: str) -> None:
        workspace_id = state["workspace"]["workspace_id"]
        result = self._call(
            state,
            2,
            actor,
            "TOOL-002",
            {
                "workspace_id": workspace_id,
                "include_paths": [self.scan_subpath],
                "follow_symlinks": False,
                "max_files": state["request"]["max_files"],
                "max_total_bytes": 1 << 40,
            },
        )
        if result is None:
            raise WorkflowError("DISCOVERY_FAILED")
        state["files"] = {file["file_id"]: {"file": file} for file in result["output"]["files"]}
        for file_id, item in state["files"].items():
            sample = self._call(
                state,
                2,
                actor,
                "TOOL-003",
                {"file_id": file_id, "mode": "prefix", "max_bytes": 65_536},
                file_id=file_id,
            )
            if sample is None:
                continue
            item["sample"] = sample["output"]["sample"]
            detected = self._call(
                state,
                2,
                actor,
                "TOOL-004",
                {"file_id": file_id, "sample_refs": [sample["output"]["sample"]["sample_id"]]},
                file_id=file_id,
            )
            if detected is not None:
                item["detection"] = detected["output"]["detection"]
                item["detection_evidence"] = detected["evidence"]

    def _stage_3(self, state: dict, actor: str) -> None:
        for file_id, item in state["files"].items():
            candidates = item.get("detection", {}).get("candidates", [])
            if not candidates:
                continue
            best = candidates[0]
            if len(candidates) > 1 and best["confidence"] - candidates[1]["confidence"] < 0.05:
                self._failure(state, 3, file_id, "AMBIGUOUS_FORMAT")
                continue
            if not any(
                evidence["evidence_id"] in best["evidence_ids"]
                and ".EXT-" not in evidence["rule_id"]
                for evidence in item["detection_evidence"]
            ):
                self._failure(state, 3, file_id, "CONTENT_EVIDENCE_REQUIRED")
                continue
            item["selected_format"] = best["format_id"]
            try:
                binding = self.gateway.extraction(best["format_id"], file_id)
            except Exception:
                self._failure(state, 3, file_id, "EXTRACTOR_UNAVAILABLE")
                continue
            if binding is None:
                self._failure(state, 3, file_id, "EXTRACTOR_UNAVAILABLE")
                continue
            tool_id, value = binding
            extracted = self._call(state, 3, actor, tool_id, value, file_id=file_id)
            if extracted is not None:
                item["extracted_metadata"] = extracted["output"]

    def _stage_4(self, state: dict, actor: str) -> None:
        for file_id, item in state["files"].items():
            if "selected_format" not in item:
                continue
            try:
                value = self.gateway.classification(file_id)
            except Exception as error:
                self._failure(
                    state, 4, file_id, str(getattr(error, "code", "METADATA_UNAVAILABLE"))
                )
                continue
            classified = self._call(state, 4, actor, "TOOL-005", value, file_id=file_id)
            if classified is not None:
                item["classification"] = classified["output"]["classification"]

    def _stage_5(self, state: dict, actor: str) -> None:
        del actor
        for file_id, item in state["files"].items():
            if "extracted_metadata" not in item and not any(
                failure["file_id"] == file_id for failure in state["failures"]
            ):
                self._failure(state, 5, file_id, "METADATA_UNAVAILABLE")

    def _stage_6(self, state: dict, actor: str) -> None:
        mutation = self.gateway.snapshot(state)
        state["inventory_mutation"] = mutation
        self.ledger.save(state)
        result = self._call(
            state,
            6,
            actor,
            "TOOL-022",
            {
                "mutation": mutation,
                "expected_state_version": state["request"]["expected_state_version"],
            },
        )
        if result is None:
            raise WorkflowError("INVENTORY_PERSISTENCE_FAILED")
        state["state_version"] = result["output"]["state_version"]

    def _stage_7(self, state: dict, actor: str) -> None:
        items = []
        offset = 0
        while True:
            result = self._call(
                state,
                7,
                actor,
                "TOOL-027",
                {
                    "inventory_id": state["request"]["inventory_id"],
                    "query": {"offset": offset, "limit": 100},
                },
            )
            if result is None:
                raise WorkflowError("REVIEW_FAILED")
            page = result["output"]
            if page["state_version"] != state["state_version"]:
                raise WorkflowError("STATE_VERSION_CONFLICT")
            records = self.gateway.review_records(
                state["request"]["inventory_id"],
                [item["file"]["file_id"] for item in page["items"]],
            )
            for item in page["items"]:
                file_id = item["file"]["file_id"]
                sample = state["files"].get(file_id, {}).get("sample")
                item["samples"] = [sample] if sample else []
                item.update(
                    records.get(
                        file_id,
                        {"detection": None, "detected_formats": [], "extracted_metadata": []},
                    )
                )
                for field, key in (("evidence", "evidence_id"), ("provenance", "provenance_id")):
                    observed = {value[key]: value for value in item.get(field, [])}
                    for value in state.get("inventory_mutation", {}).get(field, []):
                        if (
                            value.get("source_file_id") == file_id
                            or value.get("source_ref") == file_id
                        ):
                            observed[value[key]] = value
                    item[field] = list(observed.values())
                item["warnings"] = [
                    failure for failure in state["failures"] if failure["file_id"] == file_id
                ]
                if not item["extracted_metadata"] and not item["warnings"]:
                    item["warnings"].append(
                        {
                            "stage": 7,
                            "file_id": file_id,
                            "code": "METADATA_UNAVAILABLE",
                            "message": "No complete persisted extraction payload is available.",
                        }
                    )
                item["human_review_required"] = True
                items.append(item)
            offset += len(page["items"])
            if offset >= page["total_count"]:
                break
            if not page["items"]:
                raise WorkflowError("INCOMPLETE_REVIEW_PAGE")
        state["review_items"] = items
        state["review_summary"] = {key: value for key, value in page.items() if key != "items"}
