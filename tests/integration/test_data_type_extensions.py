from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from time import monotonic, sleep
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from agentic_osdu.data_types import (
    BUILTIN_DATA_TYPES,
    ClassificationProfile,
    DataTypeRegistry,
    DetectionContext,
    FormatMatch,
)
from agentic_osdu.domain.models import (
    ActorRef,
    DataCategory,
    DataSubtype,
    FormatId,
)
from agentic_osdu.formats import CancellationCheck, ExtractionOutcome, FormatSource
from agentic_osdu.runtime import create_runtime
from agentic_osdu.tools.contracts import (
    BuildInventoryReviewInput,
    DetectFormatInput,
    DiscoverFilesInput,
    JobControlAction,
    JobDefinition,
    JobStepDefinition,
    PersistInventoryInput,
    QueryOrCancelJobInput,
    ReadFileSampleInput,
    RegisterWorkspaceInput,
    SampleMode,
    ToolRequest,
    TrackJobInput,
)


def _request(workspace_id: UUID, value: BaseModel) -> ToolRequest[Any]:
    return ToolRequest[Any](
        request_id=uuid4(),
        workspace_id=workspace_id,
        actor=ActorRef(actor_id="operator"),
        input=value,
    )


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("format_id", [FormatId.TEXT, FormatId.CSV])
def test_injected_handler_drives_detection_classification_and_extraction(
    tmp_path: Path, batch: bool, format_id: FormatId
) -> None:
    calls: list[CancellationCheck | None] = []
    original = BUILTIN_DATA_TYPES.get(format_id)
    assert original is not None
    assert original.extraction is not None
    binding = original.extraction

    def detect(context: DetectionContext) -> tuple[FormatMatch, ...]:
        if context.sample.startswith(b"SURVEY NOTE"):
            return (
                FormatMatch(1.0, "SURVEY-1.SIGNATURE", "Survey note signature.", "SURVEY NOTE"),
            )
        return ()

    def classify(name: str) -> ClassificationProfile:
        assert name == "SURVEY.NOTE"
        return ClassificationProfile(
            category=DataCategory.INTERPRETATION,
            subtype=DataSubtype.HORIZON,
            osdu_kind="osdu:wks:work-product-component--SeismicHorizon:1.0.0",
        )

    def extract(
        value: BaseModel,
        source: FormatSource,
        *,
        cancellation: CancellationCheck | None = None,
    ) -> ExtractionOutcome[Any]:
        calls.append(cancellation)
        return binding.extractor(value, source, cancellation=cancellation)

    custom = replace(
        original,
        extensions=(".note",),
        detect=detect,
        classify=classify,
        extraction=replace(binding, extractor=extract),
    )
    registry = DataTypeRegistry(
        custom if item.format_id is format_id else item for item in BUILTIN_DATA_TYPES.handlers
    )
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "survey.note"
    content = b"SURVEY NOTE,VALUE\n1,2\n"
    source.write_bytes(content)
    runtime = create_runtime(tmp_path / "state.db", data_types=registry)
    try:
        registered = runtime.registry.invoke(
            "TOOL-001", _request(uuid4(), RegisterWorkspaceInput(root_path=str(root)))
        ).output
        assert registered is not None
        workspace_id = registered.workspace_id
        discovery = DiscoverFilesInput(workspace_id=workspace_id, include_paths=())
        if batch:
            inventory_id = uuid4()
            started = runtime.registry.invoke(
                "TOOL-025",
                _request(
                    workspace_id,
                    TrackJobInput(
                        workflow_id="WF-001",
                        inventory_id=inventory_id,
                        discovery=discovery,
                        definition=JobDefinition(
                            job_type="WF-001",
                            steps=tuple(
                                JobStepDefinition(
                                    sequence=index, tool_id=tool_id, input_ref="previous"
                                )
                                for index, tool_id in enumerate(
                                    ("TOOL-002", "TOOL-003", "TOOL-004", "TOOL-005", "TOOL-022"),
                                    start=1,
                                )
                            ),
                        ),
                        deduplication_key=f"extension:{uuid4()}",
                    ),
                ),
            ).output
            assert started is not None
            deadline = monotonic() + 10
            while True:
                status = runtime.registry.invoke(
                    "TOOL-026",
                    _request(
                        workspace_id,
                        QueryOrCancelJobInput(
                            action=JobControlAction.QUERY,
                            job_id=started.descriptor.job.job_id,
                        ),
                    ),
                ).output
                assert status is not None
                if status.snapshot.job.status.value in {"succeeded", "failed", "cancelled"}:
                    break
                assert monotonic() < deadline, "extension workflow did not finish"
                sleep(0.01)
            assert status.snapshot.job.status.value == "succeeded"
            view = runtime.registry.invoke(
                "TOOL-027",
                _request(workspace_id, BuildInventoryReviewInput(inventory_id=inventory_id)),
            ).output
            assert view is not None
            assert len(view.items) == 1
            classification = view.items[0].classification
            assert classification is not None
            assert classification.extraction_ids
            assert view.items[0].evidence
            assert calls[0] is not None
        else:
            discovered = runtime.registry.invoke(
                "TOOL-002", _request(workspace_id, discovery)
            ).output
            assert discovered is not None
            file = discovered.files[0]
            sample = runtime.registry.invoke(
                "TOOL-003",
                _request(
                    workspace_id,
                    ReadFileSampleInput(
                        file_id=file.file_id, mode=SampleMode.PREFIX, max_bytes=1024
                    ),
                ),
            ).output
            assert sample is not None
            detected = runtime.registry.invoke(
                "TOOL-004",
                _request(
                    workspace_id,
                    DetectFormatInput(file_id=file.file_id, sample_refs=(sample.sample.sample_id,)),
                ),
            ).output
            assert detected is not None
            assert detected.detection.candidates[0].confidence == 1
            assert custom.extraction is not None
            extracted = runtime.registry.invoke(
                custom.extraction.tool_id,
                _request(workspace_id, custom.extraction.build_input(file.file_id)),
            )
            assert extracted.output is not None
            result = runtime.registry.invoke(
                "TOOL-005",
                _request(
                    workspace_id,
                    runtime.classification_input(file.file_id),
                ),
            ).output
            assert result is not None
            classification = result.classification
            assert classification.extraction_ids
            inventory_id = uuid4()
            snapshot = runtime.inventory_snapshot(inventory_id, (file.file_id,))
            assert snapshot.extractions[0].extraction_id in classification.extraction_ids
            assert snapshot.extracted_metadata[0].payload == extracted.output
            assert snapshot.files[0].sha256 is not None
            persisted = runtime.registry.invoke(
                "TOOL-022",
                _request(
                    workspace_id,
                    PersistInventoryInput(mutation=snapshot, expected_state_version=0),
                ),
            ).output
            assert persisted is not None
            assert persisted.state_version == 1
            restored = runtime.repository.load_runtime_file_records(
                inventory_id=inventory_id, file_ids=(file.file_id,)
            )
            assert restored[0][1].metadata_extractions == snapshot.extracted_metadata
            assert (
                runtime.repository.load_runtime_file_records(inventory_id=inventory_id, file_ids=())
                == ()
            )
        assert classification.category is DataCategory.INTERPRETATION
        assert classification.subtype is DataSubtype.HORIZON
        assert len(calls) == 1
        assert source.read_bytes() == content
        assert BUILTIN_DATA_TYPES.get(format_id) is original
        assert ".note" not in original.extensions
    finally:
        runtime.database.dispose()
