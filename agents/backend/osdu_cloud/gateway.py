"""Adapt stage operations to the same typed registry used by local MCP."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid5

from agentic_osdu.domain.models import EvidenceRecord, ProvenanceRecord, TrustLevel
from agentic_osdu.runtime import RuntimeComposition
from agentic_osdu.tools.contracts import TOOL_REGISTRY, FormatId, ToolRequest


class ToolGateway:
    def __init__(self, runtime: RuntimeComposition) -> None:
        self.runtime = runtime

    def call(
        self, tool_id: str, request_id: str, workspace_id: str, actor: str, value: dict
    ) -> dict:
        definition = TOOL_REGISTRY[tool_id]
        request_type = ToolRequest[definition.input_model]
        request = request_type.model_validate_json(
            json.dumps(
                {
                    "request_id": request_id,
                    "workspace_id": workspace_id,
                    "actor": {"actor_id": f"entra:{actor}"},
                    "input": value,
                }
            )
        )
        return self.runtime.registry.invoke(tool_id, request).model_dump(mode="json")

    def extraction(self, format_id: str, file_id: str) -> tuple[str, dict] | None:
        handler = self.runtime.data_types.get(FormatId(format_id))
        if handler is None or handler.extraction is None:
            return None
        binding = handler.extraction
        return binding.tool_id, binding.build_input(UUID(file_id)).model_dump(mode="json")

    def classification(self, file_id: str) -> dict:
        return self.runtime.classification_input(UUID(file_id)).model_dump(mode="json")

    def snapshot(self, state: dict) -> dict:
        run_id = UUID(state["request"]["run_id"])
        file_ids = tuple(UUID(file_id) for file_id in state["files"])
        evidence = tuple(
            EvidenceRecord(
                evidence_id=uuid5(run_id, json.dumps(failure, sort_keys=True)),
                evidence_type="workflow_failure",
                rule_id=f"FOUNDRY-STAGE-{failure['stage']}",
                summary=failure["message"],
                observed_value=failure["code"],
                source_file_id=UUID(failure["file_id"]),
                trust_level=TrustLevel.UNTRUSTED,
            )
            for failure in state["failures"]
            if failure["file_id"] is not None
        )
        provenance = tuple(
            ProvenanceRecord(
                provenance_id=uuid5(run_id, f"failure-accounting:{file_id}"),
                source_type="foundry_exploration",
                source_ref=str(file_id),
                tool_id="TOOL-022",
                tool_version=TOOL_REGISTRY["TOOL-022"].version,
                recorded_at=datetime.now(UTC),
                transformation=f"Retained per-file failure accounting for run {run_id}.",
            )
            for file_id in file_ids
            if any(failure["file_id"] == str(file_id) for failure in state["failures"])
        )
        inventory_id = UUID(state["request"]["inventory_id"])
        snapshot = self.runtime.inventory_snapshot(
            inventory_id,
            file_ids,
            additional_evidence=evidence,
            additional_provenance=provenance,
        )
        selected_paths = {file.relative_path.root for file in snapshot.files}
        old_ids = tuple(
            record.file.file_id
            for _, record in self.runtime.repository.load_runtime_file_records(
                inventory_id=inventory_id
            )
            if record.file.relative_path.root in selected_paths
            and record.file.file_id not in file_ids
        )
        return snapshot.model_copy(update={"remove_file_ids": old_ids}).model_dump(mode="json")

    def review_records(self, inventory_id: str, file_ids: list[str]) -> dict:
        return {
            str(record.file.file_id): {
                "detection": record.detection.model_dump(mode="json") if record.detection else None,
                "detected_formats": [
                    candidate.format_id.name for candidate in record.detection.candidates
                ]
                if record.detection
                else [],
                "extracted_metadata": [
                    metadata.model_dump(mode="json") for metadata in record.metadata_extractions
                ],
            }
            for _, record in self.runtime.repository.load_runtime_file_records(
                inventory_id=UUID(inventory_id), file_ids=tuple(UUID(value) for value in file_ids)
            )
        }
