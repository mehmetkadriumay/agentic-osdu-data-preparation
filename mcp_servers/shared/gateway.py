"""Reuse the domain tool registry; MCP and agents never manufacture metadata."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

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
        request = ToolRequest[definition.input_model].model_validate_json(
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

    def snapshot(self, inventory_id: str, files: dict) -> dict:
        evidence = tuple(
            EvidenceRecord(
                evidence_id=uuid4(),
                evidence_type="operation_failure",
                rule_id=f"MCP-{failure['operation'].upper()}",
                summary="The requested metadata operation did not establish a complete result.",
                observed_value=failure["code"],
                source_file_id=UUID(file_id),
                trust_level=TrustLevel.UNTRUSTED,
            )
            for file_id, item in files.items()
            for failure in item["warnings"]
        )
        provenance = tuple(
            ProvenanceRecord(
                provenance_id=uuid4(),
                source_type="mcp_failure_accounting",
                source_ref=file_id,
                tool_id="TOOL-022",
                tool_version=TOOL_REGISTRY["TOOL-022"].version,
                recorded_at=datetime.now(UTC),
                transformation="Preserved independent operation failures in the inventory.",
            )
            for file_id, item in files.items()
            if item["warnings"]
        )
        file_ids = tuple(UUID(file_id) for file_id in files)
        snapshot = self.runtime.inventory_snapshot(
            UUID(inventory_id),
            file_ids,
            additional_evidence=evidence,
            additional_provenance=provenance,
        )
        paths = {file.relative_path.root for file in snapshot.files}
        old_ids = tuple(
            record.file.file_id
            for _, record in self.runtime.repository.load_runtime_file_records(
                inventory_id=UUID(inventory_id)
            )
            if record.file.relative_path.root in paths and record.file.file_id not in file_ids
        )
        return snapshot.model_copy(update={"remove_file_ids": old_ids}).model_dump(mode="json")

    def review_records(self, inventory_id: str, file_ids: list[str]) -> dict:
        return {
            str(record.file.file_id): {
                "detection": record.detection.model_dump(mode="json") if record.detection else None,
                "extracted_metadata": [
                    metadata.model_dump(mode="json") for metadata in record.metadata_extractions
                ],
            }
            for _, record in self.runtime.repository.load_runtime_file_records(
                inventory_id=UUID(inventory_id), file_ids=tuple(UUID(value) for value in file_ids)
            )
        }
