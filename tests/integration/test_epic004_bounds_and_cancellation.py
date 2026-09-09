from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from agentic_osdu.formats import BytesFormatSource, FormatExtractionError
from agentic_osdu.formats.interpretation import extract_interpretation
from agentic_osdu.formats.supporting import extract_csv
from agentic_osdu.formats.well_logs import extract_json_well_log
from agentic_osdu.tools.contracts import (
    CsvExtractionOptions,
    DiscoverFilesInput,
    ExtractCsvInput,
    ExtractInterpretationInput,
    ExtractJsonWellLogInput,
    InterpretationExtractionOptions,
    InterpretationSubtype,
    JsonWellLogOptions,
    RegisterWorkspaceInput,
)
from agentic_osdu.tools.discovery import (
    DiscoveryError,
    DiscoveryService,
    InMemoryWorkspacePolicyStore,
)


def test_json_stream_stops_at_row_bound_without_materializing_remaining_rows() -> None:
    document = [
        {
            "header": {"name": "bounded"},
            "curves": [{"name": "INDEX", "valueType": "integer"}],
            "data": [[index] for index in range(20)],
        }
    ]
    item = BytesFormatSource(file_id=uuid4(), content=json.dumps(document).encode())

    with pytest.raises(FormatExtractionError, match="JSON_LIMIT_EXCEEDED"):
        extract_json_well_log(
            ExtractJsonWellLogInput(
                file_id=item.file_id,
                options=JsonWellLogOptions(max_rows=5),
            ),
            item,
        )


def test_csv_and_text_sampling_report_truncation_at_configured_bounds() -> None:
    csv_item = BytesFormatSource(file_id=uuid4(), content=b"a,b\n1,2\n3,4\n5,6\n")
    csv_result = extract_csv(
        ExtractCsvInput(
            file_id=csv_item.file_id,
            options=CsvExtractionOptions(max_sample_rows=2),
        ),
        csv_item,
    )
    assert csv_result.output.sampled_row_count == 2
    assert [row.row_number for row in csv_result.output.sampled_rows] == [2, 3]

    text_item = BytesFormatSource(file_id=uuid4(), content=b"one\ntwo\nthree\n")
    text_result = extract_interpretation(
        ExtractInterpretationInput(
            file_id=text_item.file_id,
            subtype=InterpretationSubtype.TEXT,
            options=InterpretationExtractionOptions(max_rows=2),
        ),
        text_item,
    )
    assert text_result.output.text is not None
    assert text_result.output.text.line_count == 2
    assert text_result.output.text.truncated is True


def test_cancellation_is_checked_between_streamed_rows() -> None:
    document = [
        {
            "header": {"name": "cancel"},
            "curves": [{"name": "INDEX", "valueType": "integer"}],
            "data": [[index] for index in range(10)],
        }
    ]
    item = BytesFormatSource(file_id=uuid4(), content=json.dumps(document).encode())
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks > 4

    with pytest.raises(FormatExtractionError, match="CANCELLED"):
        extract_json_well_log(
            ExtractJsonWellLogInput(file_id=item.file_id),
            item,
            cancellation=cancelled,
        )


def test_extractors_consume_only_discovered_read_only_file_capabilities(tmp_path: Path) -> None:
    path = tmp_path / "sample.csv"
    path.write_text("a,b\n1,2\n", encoding="ascii")
    service = DiscoveryService(store=InMemoryWorkspacePolicyStore())
    workspace = service.register_workspace(RegisterWorkspaceInput(root_path=str(tmp_path)))
    asset = service.discover_files(DiscoverFilesInput(workspace_id=workspace.workspace_id)).files[0]

    capability = service.format_source(asset.file_id)
    result = extract_csv(ExtractCsvInput(file_id=capability.file_id), capability)
    assert result.output.headers == ("a", "b")

    path.write_text("changed", encoding="ascii")
    with pytest.raises(DiscoveryError, match="FILE_CHANGED"):
        extract_csv(ExtractCsvInput(file_id=capability.file_id), capability)
