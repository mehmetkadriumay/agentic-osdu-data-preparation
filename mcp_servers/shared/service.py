"""Independent operations over authoritative observations, with commit barriers."""

from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from copy import deepcopy
from typing import Protocol
from uuid import UUID, uuid4

ROLE_OPERATIONS = {
    1: frozenset({"register_source_workspace"}),
    2: frozenset({"discover_source_files", "inspect_file_format"}),
    3: frozenset({"extract_detected_metadata"}),
    4: frozenset({"classify_observed_file"}),
    5: frozenset({"account_file_failures"}),
    6: frozenset({"persist_observed_inventory"}),
    7: frozenset({"review_persisted_inventory"}),
}
SERVER_ROLES = {
    "workspace-discovery": frozenset({1, 2}),
    "extraction": frozenset({3}),
    "classification": frozenset({4}),
    "inventory-review": frozenset({5, 6, 7}),
}


class OperationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Gateway(Protocol):
    def call(
        self, tool_id: str, request_id: str, workspace_id: str, actor: str, value: dict
    ) -> dict: ...
    def extraction(self, format_id: str, file_id: str) -> tuple[str, dict] | None: ...
    def classification(self, file_id: str) -> dict: ...
    def snapshot(self, inventory_id: str, files: dict) -> dict: ...
    def review_records(self, inventory_id: str, file_ids: list[str]) -> dict: ...


class Ledger(Protocol):
    def get(self, request_id: str) -> dict | None: ...
    def save(self, receipt: dict) -> None: ...
    def inventory_receipt(self, inventory_id: str) -> dict | None: ...


class CommitBarrier:
    """Allow independent file work; block snapshots/discovery until all work drains."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.active = 0
        self.writers = 0
        self.writing = False

    @contextmanager
    def enter(self, *, exclusive: bool):
        with self.condition:
            if exclusive:
                self.writers += 1
                self.condition.wait_for(lambda: not self.active and not self.writing)
                self.writers -= 1
                self.writing = True
            else:
                self.condition.wait_for(lambda: not self.writing and not self.writers)
                self.active += 1
        try:
            yield
        finally:
            with self.condition:
                if exclusive:
                    self.writing = False
                else:
                    self.active -= 1
                self.condition.notify_all()


class OperationService:
    def __init__(
        self,
        gateway: Gateway,
        ledger: Ledger,
        source_root: str,
        scan_subpath: str = "Data",
        max_files: int = 100,
    ) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.source_root = source_root
        self.scan_subpath = scan_subpath
        self.max_files = max_files
        self.instance_id = str(uuid4())
        self.workspace: dict | None = None
        self.files: dict[str, dict] = {}
        self.discovery_result: dict | None = None
        self.barrier = CommitBarrier()
        self.lock = threading.Lock()
        self.request_locks: dict[str, threading.Lock] = {}
        self.file_locks: dict[str, threading.Lock] = {}

    def _mutex(self, collection: dict, key: str) -> threading.Lock:
        with self.lock:
            return collection.setdefault(key, threading.Lock())

    def execute(
        self, operation: str, request_id: str, arguments: dict, principal_id: str, role: int
    ) -> dict:
        if operation not in ROLE_OPERATIONS.get(role, ()):
            raise OperationError("CAPABILITY_DENIED")
        request_id = str(UUID(request_id))
        arguments = deepcopy(arguments)
        with self._mutex(self.request_locks, request_id):
            old = self.ledger.get(request_id)
            if old:
                if (old["operation"], old["arguments"], old["principal_id"]) != (
                    operation,
                    arguments,
                    principal_id,
                ):
                    raise OperationError("REQUEST_CONFIGURATION_CONFLICT")
                if old["status"] == "in_progress":
                    raise OperationError("OPERATION_OUTCOME_UNKNOWN")
                return old
            receipt = {
                "request_id": request_id,
                "operation": operation,
                "arguments": arguments,
                "principal_id": principal_id,
                "instance_id": self.instance_id,
                "status": "in_progress",
            }
            self.ledger.save(receipt)
            exclusive = operation in {
                "register_source_workspace",
                "discover_source_files",
                "account_file_failures",
                "persist_observed_inventory",
                "review_persisted_inventory",
            }
            with self.barrier.enter(exclusive=exclusive):
                file_id = arguments.get("file_id")
                mutex = self._mutex(self.file_locks, str(file_id)) if file_id else self.lock
                with mutex:
                    try:
                        result = getattr(self, f"_{operation}")(request_id, principal_id, arguments)
                        receipt.update(status="succeeded", result=result)
                    except Exception as error:
                        code = str(getattr(error, "code", "TOOL_EXECUTION_FAILED"))
                        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
                            code = "TOOL_EXECUTION_FAILED"
                        receipt.update(status="failed", error={"code": code})
                        if file_id in self.files:
                            failure = {"operation": operation, "file_id": file_id, "code": code}
                            if failure not in self.files[file_id]["warnings"]:
                                self.files[file_id]["warnings"].append(failure)
            self.ledger.save(receipt)
            return receipt

    def _call(self, tool_id: str, request_id: str, actor: str, value: dict) -> dict:
        result = self.gateway.call(
            tool_id,
            request_id,
            self.workspace["workspace_id"] if self.workspace else str(UUID(int=0)),
            actor,
            value,
        )
        if result["status"] not in {"succeeded", "partially_succeeded"} or not result.get("output"):
            errors = result.get("errors", [])
            raise OperationError(errors[0]["code"] if errors else "TOOL_RESULT_UNAVAILABLE")
        return result

    @staticmethod
    def _fields(value: dict, allowed: set[str]) -> None:
        if not isinstance(value, dict) or set(value) - allowed:
            raise OperationError("OPERATION_INPUT_INVALID")

    def _file(self, value: dict) -> tuple[str, dict]:
        self._fields(value, {"file_id"})
        file_id = str(UUID(value["file_id"]))
        if file_id not in self.files:
            raise OperationError("FILE_CAPABILITY_EXPIRED")
        return file_id, self.files[file_id]

    def _selection(self, value: dict) -> dict:
        ids = value["file_ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= self.max_files:
            raise OperationError("FILE_SELECTION_INVALID")
        keys = [str(UUID(file_id)) for file_id in ids]
        if len(set(keys)) != len(keys) or not set(keys) <= self.files.keys():
            raise OperationError("FILE_CAPABILITY_EXPIRED")
        return {key: self.files[key] for key in keys}

    def _register_source_workspace(self, request_id: str, actor: str, value: dict) -> dict:
        self._fields(value, set())
        result = self._call(
            "TOOL-001",
            request_id,
            actor,
            {
                "root_path": self.source_root,
                "read_only": True,
                "allowed_output_subpaths": [],
            },
        )
        self.workspace = result["output"]
        return result

    def _discover_source_files(self, request_id: str, actor: str, value: dict) -> dict:
        self._fields(value, {"max_files"})
        if self.workspace is None:
            raise OperationError("WORKSPACE_REQUIRED")
        maximum = value.get("max_files", self.max_files)
        if type(maximum) is not int or not 1 <= maximum <= self.max_files:
            raise OperationError("DISCOVERY_BUDGET_INVALID")
        if self.discovery_result:
            if maximum != self.discovery_result["budget"]:
                raise OperationError("DISCOVERY_BUDGET_CONFLICT")
            return deepcopy(self.discovery_result["result"])
        result = self._call(
            "TOOL-002",
            request_id,
            actor,
            {
                "workspace_id": self.workspace["workspace_id"],
                "include_paths": [self.scan_subpath],
                "follow_symlinks": False,
                "max_files": maximum,
                "max_total_bytes": 1 << 40,
            },
        )
        self.files = {
            file["file_id"]: {"file": file, "warnings": []} for file in result["output"]["files"]
        }
        self.discovery_result = {"budget": maximum, "result": deepcopy(result)}
        return result

    def _inspect_file_format(self, request_id: str, actor: str, value: dict) -> dict:
        file_id, item = self._file(value)
        sampled = self._call(
            "TOOL-003",
            request_id,
            actor,
            {
                "file_id": file_id,
                "mode": "prefix",
                "max_bytes": 65_536,
            },
        )
        item["sample"] = sampled["output"]["sample"]
        result = self._call(
            "TOOL-004",
            request_id,
            actor,
            {
                "file_id": file_id,
                "sample_refs": [item["sample"]["sample_id"]],
            },
        )
        item["detection"] = result["output"]["detection"]
        item["detection_evidence"] = result["evidence"]
        return result | {"sample": item["sample"]}

    @staticmethod
    def _format(item: dict) -> str:
        candidates = item.get("detection", {}).get("candidates", [])
        if not candidates:
            raise OperationError("DETECTION_REQUIRED")
        best = candidates[0]
        if len(candidates) > 1 and best["confidence"] - candidates[1]["confidence"] < 0.05:
            raise OperationError("AMBIGUOUS_FORMAT")
        if not any(
            evidence["evidence_id"] in best["evidence_ids"] and ".EXT-" not in evidence["rule_id"]
            for evidence in item.get("detection_evidence", [])
        ):
            raise OperationError("CONTENT_EVIDENCE_REQUIRED")
        return best["format_id"]

    def _extract_detected_metadata(self, request_id: str, actor: str, value: dict) -> dict:
        file_id, item = self._file(value)
        binding = self.gateway.extraction(self._format(item), file_id)
        if binding is None:
            raise OperationError("UNSUPPORTED_FORMAT")
        tool_id, arguments = binding
        result = self._call(tool_id, request_id, actor, arguments)
        item["extraction"] = result
        return result

    def _classify_observed_file(self, request_id: str, actor: str, value: dict) -> dict:
        file_id, item = self._file(value)
        self._format(item)
        result = self._call("TOOL-005", request_id, actor, self.gateway.classification(file_id))
        item["classification"] = result
        return result

    @staticmethod
    def _account(files: dict) -> list[dict]:
        warnings = []
        for file_id, item in files.items():
            for field, code in (
                ("extraction", "METADATA_UNAVAILABLE"),
                ("classification", "CLASSIFICATION_UNAVAILABLE"),
            ):
                if field in item:
                    item["warnings"][:] = [
                        w
                        for w in item["warnings"]
                        if not (w["operation"] == "account_file_failures" and w["code"] == code)
                    ]
                if field not in item and not any(w["code"] == code for w in item["warnings"]):
                    item["warnings"].append(
                        {
                            "operation": "account_file_failures",
                            "file_id": file_id,
                            "code": code,
                        }
                    )
            warnings.extend(item["warnings"])
        return deepcopy(warnings)

    def _account_file_failures(self, request_id: str, actor: str, value: dict) -> dict:
        del request_id, actor
        self._fields(value, {"file_ids"})
        files = self._selection(value)
        return {
            "file_ids": list(files),
            "warnings": self._account(files),
            "human_review_required": True,
        }

    def _persist_observed_inventory(self, request_id: str, actor: str, value: dict) -> dict:
        self._fields(value, {"file_ids", "inventory_id", "expected_state_version"})
        files = self._selection(value)
        version = value.get("expected_state_version", 0)
        if type(version) is not int or version < 0 or (not value.get("inventory_id") and version):
            raise OperationError("STATE_VERSION_INVALID")
        inventory_id = (
            str(UUID(value["inventory_id"])) if value.get("inventory_id") else str(uuid4())
        )
        self._account(files)
        mutation = self.gateway.snapshot(inventory_id, files)
        result = self._call(
            "TOOL-022",
            request_id,
            actor,
            {
                "mutation": mutation,
                "expected_state_version": version,
            },
        )
        return result | {"inventory_id": inventory_id, "snapshot": mutation}

    def _review_persisted_inventory(self, request_id: str, actor: str, value: dict) -> dict:
        self._fields(value, {"inventory_id", "offset", "limit"})
        inventory_id = str(UUID(value["inventory_id"]))
        offset, limit = value.get("offset", 0), value.get("limit", 100)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise OperationError("REVIEW_PAGE_INVALID")
        result = self._call(
            "TOOL-027",
            request_id,
            actor,
            {
                "inventory_id": inventory_id,
                "query": {"offset": offset, "limit": limit},
            },
        )
        page = result["output"]
        committed = self.ledger.inventory_receipt(inventory_id)
        mutation = {}
        if committed:
            if committed["result"]["output"]["state_version"] != page["state_version"]:
                raise OperationError("STATE_VERSION_CONFLICT")
            mutation = committed["result"]["snapshot"]
        records = self.gateway.review_records(
            inventory_id,
            [item["file"]["file_id"] for item in page["items"]],
        )
        for item in page["items"]:
            file_id = item["file"]["file_id"]
            item.update(records.get(file_id, {"extracted_metadata": [], "detection": None}))
            for field, key in (("evidence", "evidence_id"), ("provenance", "provenance_id")):
                observed = {entry[key]: entry for entry in item.get(field, [])}
                for entry in mutation.get(field, []):
                    if entry.get("source_file_id") == file_id or entry.get("source_ref") == file_id:
                        observed[entry[key]] = entry
                item[field] = list(observed.values())
            item["warnings"] = [
                entry
                for entry in item["evidence"]
                if entry.get("evidence_type") == "operation_failure"
            ]
            if not committed:
                item["warnings"].append(
                    {
                        "code": "OPERATION_RECEIPT_UNAVAILABLE",
                        "message": "Operation snapshot unavailable; evidence may be incomplete.",
                    }
                )
            if not item.get("extracted_metadata"):
                item["warnings"].append(
                    {
                        "code": "METADATA_UNAVAILABLE",
                        "message": "No complete persisted extraction payload is available.",
                    }
                )
            item["human_review_required"] = True
        return result | {"inventory_id": inventory_id}
