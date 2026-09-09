from __future__ import annotations

import json
import struct
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentic_osdu.domain.models import WorkspaceRelativePath
from agentic_osdu.formats import BytesFormatSource, FormatExtractionError
from agentic_osdu.formats.interpretation import extract_interpretation
from agentic_osdu.formats.navigation import extract_p190
from agentic_osdu.formats.segy import extract_segy
from agentic_osdu.formats.supporting import extract_csv
from agentic_osdu.formats.well_logs import (
    extract_dlis,
    extract_json_well_log,
    extract_lis,
)
from agentic_osdu.tools.contracts import (
    CsvExtractionOptions,
    DlisExtractionOptions,
    ExtractCsvInput,
    ExtractDlisInput,
    ExtractInterpretationInput,
    ExtractJsonWellLogInput,
    ExtractLisInput,
    ExtractP190Input,
    ExtractSegyInput,
    InterpretationExtractionOptions,
    InterpretationSubtype,
    JsonWellLogOptions,
    LisExtractionOptions,
    SegyExtractionOptions,
)


def _source(content: bytes) -> BytesFormatSource:
    return BytesFormatSource(file_id=uuid4(), content=content)


def _little_segy(trace_count: int = 2) -> bytes:
    binary = bytearray(400)
    struct.pack_into("<H", binary, 16, 4000)
    struct.pack_into("<H", binary, 20, 1)
    struct.pack_into("<H", binary, 24, 3)
    traces = bytearray()
    for index in range(trace_count):
        trace = bytearray(242)
        struct.pack_into("<H", trace, 114, 1)
        struct.pack_into("<i", trace, 188, index + 1)
        traces.extend(trace)
    return b"C 1 TIME".ljust(3200) + binary + traces


def test_segy_little_endian_hint_sampling_and_truncation() -> None:
    item = _source(_little_segy())
    result = extract_segy(
        ExtractSegyInput(
            file_id=item.file_id,
            options=SegyExtractionOptions(max_trace_samples=1, endian_hint="little"),
        ),
        item,
    )
    assert result.output.endian == "little"
    assert result.output.sampled_trace_count == 1
    assert result.output.dimensions.trace_count == 2

    truncated = _source(_little_segy() + b"x")
    with pytest.raises(FormatExtractionError, match="SEGY_TRACE_TRUNCATED"):
        extract_segy(
            ExtractSegyInput(
                file_id=truncated.file_id,
                options=SegyExtractionOptions(endian_hint="little"),
            ),
            truncated,
        )


def test_csv_dialect_bom_hint_and_empty_failures() -> None:
    bom = _source(b"\xef\xbb\xbfa|b\n1|2\n")
    result = extract_csv(
        ExtractCsvInput(
            file_id=bom.file_id,
            options=CsvExtractionOptions(delimiter_hint="|"),
        ),
        bom,
    )
    assert result.output.headers == ("a", "b")

    empty = _source(b"")
    with pytest.raises(FormatExtractionError, match="CSV_DIALECT_UNKNOWN"):
        extract_csv(ExtractCsvInput(file_id=empty.file_id), empty)

    unknown = _source(b"single-column\nvalue\n")
    with pytest.raises(FormatExtractionError, match="CSV_DIALECT_UNKNOWN"):
        extract_csv(ExtractCsvInput(file_id=unknown.file_id), unknown)


def test_p190_southern_utm_and_ascii_failure() -> None:
    headers = "\n".join(
        (
            "H1800" + (" " * 27) + "UTM",
            "H1400" + (" " * 27) + "WGS 84",
            "H1900" + (" " * 27) + "56 SOUTH",
        )
    )
    position = (
        "S"
        + "SOUTH".ljust(12)
        + (" " * 6)
        + "000001"
        + "300000.00S"
        + "1500000.00E"
        + "0500000.0"
        + "6000000.0"
    )
    item = _source(f"{headers}\n{position}\n".encode())
    assert extract_p190(ExtractP190Input(file_id=item.file_id), item).output.inferred_epsg == 32756

    invalid = _source(b"\xff")
    with pytest.raises(FormatExtractionError, match="P190_HEADER_INVALID"):
        extract_p190(ExtractP190Input(file_id=invalid.file_id), invalid)


def test_tool_013_fault_companion_and_subtype_bounds() -> None:
    fault = [" "] * 120
    fault[2:12] = f"{100.0:10.1f}"
    fault[13:24] = f"{200.0:11.1f}"
    fault[25:36] = f"{300.0:11.1f}"
    fault[52:100] = "FAULT-A".ljust(48)
    item = _source(("".join(fault) + "\n").encode("latin-1"))
    companion = BytesFormatSource(
        file_id=uuid4(),
        content=b"type: UTM\nzone: 31N\n",
        relative_path=WorkspaceRelativePath("README.txt"),
    )
    request = ExtractInterpretationInput(
        file_id=item.file_id,
        subtype=InterpretationSubtype.DAT,
        options=InterpretationExtractionOptions(
            companion_crs_path=WorkspaceRelativePath("README.txt"),
        ),
    )
    result = extract_interpretation(request, item, companion_source=companion)
    assert result.output.dat is not None
    assert result.output.dat.interpretation_type == "fault"
    assert result.output.dat.crs == "UTM / 31N"

    with pytest.raises(FormatExtractionError, match="COMPANION_CRS_INVALID"):
        extract_interpretation(request, item)

    bad_companion = BytesFormatSource(
        file_id=uuid4(),
        content=b"no projection",
        relative_path=WorkspaceRelativePath("README.txt"),
    )
    with pytest.raises(FormatExtractionError, match="COMPANION_CRS_INVALID"):
        extract_interpretation(request, item, companion_source=bad_companion)

    inconsistent = _source(b"H 1 1 1 1\n1 2\n1 2 3\n")
    with pytest.raises(FormatExtractionError, match="SGP_INVALID"):
        extract_interpretation(
            ExtractInterpretationInput(
                file_id=inconsistent.file_id,
                subtype=InterpretationSubtype.SGP,
            ),
            inconsistent,
        )


@pytest.mark.parametrize(
    ("document", "code"),
    [
        ({"header": {"name": "x"}, "curves": [], "data": []}, "JSON_INVALID"),
        (
            [
                {
                    "header": {},
                    "curves": [{"name": "I"}],
                    "data": [[1]],
                }
            ],
            "JSON_INVALID",
        ),
        (
            [
                {
                    "header": {"name": "x"},
                    "curves": [{"name": "I"}, {"name": "V"}],
                    "data": [[1]],
                }
            ],
            "CURVE_SCHEMA_INVALID",
        ),
        (
            [
                {
                    "header": {"name": "x", "endIndex": 9},
                    "curves": [{"name": "I"}],
                    "data": [[1]],
                }
            ],
            "INDEX_INCONSISTENT",
        ),
    ],
)
def test_json_well_log_additional_structure_failures(
    document: object,
    code: str,
) -> None:
    item = _source(json.dumps(document).encode())
    with pytest.raises(FormatExtractionError, match=code):
        extract_json_well_log(ExtractJsonWellLogInput(file_id=item.file_id), item)


def test_json_well_log_multidimensional_and_curve_bound() -> None:
    document = [
        {
            "header": {"name": "typed"},
            "curves": [
                {"name": "I", "valueType": "integer"},
                {"name": "FLAGS", "valueType": "boolean", "dimensions": 2},
                {"name": "LABEL", "valueType": "string"},
            ],
            "data": [[1, [True, False], "a"]],
        }
    ]
    item = _source(json.dumps(document).encode())
    result = extract_json_well_log(
        ExtractJsonWellLogInput(file_id=item.file_id),
        item,
    )
    assert result.output.column_count == 3

    with pytest.raises(FormatExtractionError, match="CURVE_SCHEMA_INVALID"):
        extract_json_well_log(
            ExtractJsonWellLogInput(
                file_id=item.file_id,
                options=JsonWellLogOptions(max_curves=2),
            ),
            item,
        )


def test_native_logical_file_limits_empty_results_and_lis_errors() -> None:
    item = _source(b"native")
    logical = SimpleNamespace(
        fileheader=SimpleNamespace(id="LF"),
        frames=[],
        channels=[],
        origins=[],
    )
    with pytest.raises(FormatExtractionError, match="DLIS_LOGICAL_FILE_INVALID"):
        extract_dlis(
            ExtractDlisInput(
                file_id=item.file_id,
                options=DlisExtractionOptions(max_logical_files=1),
            ),
            item,
            loader=lambda _: [logical, logical],
            library_version="test",
        )
    with pytest.raises(FormatExtractionError, match="DLIS_LOGICAL_FILE_INVALID"):
        extract_dlis(
            ExtractDlisInput(file_id=item.file_id),
            item,
            loader=lambda _: [],
            library_version="test",
        )

    class EmptyLis:
        def header(self) -> SimpleNamespace:
            return SimpleNamespace()

        def data_format_specs(self) -> list[object]:
            return []

    with pytest.raises(FormatExtractionError, match="LIS_RECORD_INVALID"):
        extract_lis(
            ExtractLisInput(file_id=item.file_id),
            item,
            loader=lambda _: [EmptyLis()],
        )

    class TwoLis:
        def header(self) -> SimpleNamespace:
            return SimpleNamespace()

        def data_format_specs(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(specs=[]),
                SimpleNamespace(specs=[]),
            ]

    with pytest.raises(FormatExtractionError, match="LIS_TRUNCATED"):
        extract_lis(
            ExtractLisInput(
                file_id=item.file_id,
                options=LisExtractionOptions(max_records=1),
            ),
            item,
            loader=lambda _: [TwoLis()],
        )
