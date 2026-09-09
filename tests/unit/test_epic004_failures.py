from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentic_osdu.formats import BytesFormatSource, FormatExtractionError
from agentic_osdu.formats.interpretation import extract_interpretation
from agentic_osdu.formats.navigation import extract_p190
from agentic_osdu.formats.segy import extract_segy
from agentic_osdu.formats.supporting import extract_csv
from agentic_osdu.formats.well_logs import (
    extract_dlis,
    extract_json_well_log,
    extract_las,
    extract_lis,
)
from agentic_osdu.tools.contracts import (
    CsvExtractionOptions,
    DlisExtractionOptions,
    ExtractCsvInput,
    ExtractDlisInput,
    ExtractInterpretationInput,
    ExtractJsonWellLogInput,
    ExtractLasInput,
    ExtractLisInput,
    ExtractP190Input,
    ExtractSegyInput,
    InterpretationSubtype,
    LasExtractionOptions,
    LisExtractionOptions,
)


def test_segy_malformed_truncated_and_ambiguous_fail_with_stable_codes() -> None:
    short = BytesFormatSource(file_id=uuid4(), content=b"C 1 short")
    with pytest.raises(FormatExtractionError, match="SEGY_HEADER_INVALID") as failure:
        extract_segy(ExtractSegyInput(file_id=short.file_id), short)
    assert failure.value.code == "SEGY_HEADER_INVALID"

    ambiguous = BytesFormatSource(file_id=uuid4(), content=b" " * 3600)
    with pytest.raises(FormatExtractionError, match="SEGY_ENDIAN_AMBIGUOUS"):
        extract_segy(ExtractSegyInput(file_id=ambiguous.file_id), ambiguous)


def test_las_sections_encoding_and_line_bound_are_enforced() -> None:
    invalid = BytesFormatSource(file_id=uuid4(), content=b"not las")
    with pytest.raises(FormatExtractionError, match="LAS_SECTION_INVALID"):
        extract_las(ExtractLasInput(file_id=invalid.file_id), invalid)

    encoded = BytesFormatSource(file_id=uuid4(), content=b"~V\nVERS. 2.0 : \xff")
    with pytest.raises(FormatExtractionError, match="LAS_ENCODING_FAILED"):
        extract_las(
            ExtractLasInput(
                file_id=encoded.file_id,
                options=LasExtractionOptions(encoding="utf-8"),
            ),
            encoded,
        )

    too_many = BytesFormatSource(file_id=uuid4(), content=b"~V\nVERS. 2.0 : x\n~W\nWELL. : x\n")
    with pytest.raises(FormatExtractionError, match="LAS_SECTION_INVALID"):
        extract_las(
            ExtractLasInput(
                file_id=too_many.file_id,
                options=LasExtractionOptions(max_lines=2),
            ),
            too_many,
        )


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (b"{", "JSON_INVALID"),
        (
            json.dumps([{"header": {"name": "x"}, "curves": [{}], "data": [[1]]}]).encode(),
            "CURVE_SCHEMA_INVALID",
        ),
        (
            json.dumps(
                [
                    {
                        "header": {"name": "x"},
                        "curves": [{"name": "I", "valueType": "integer"}],
                        "data": [["bad"]],
                    }
                ]
            ).encode(),
            "CELL_TYPE_MISMATCH",
        ),
        (
            json.dumps(
                [
                    {
                        "header": {"name": "x", "startIndex": 1, "step": 1},
                        "curves": [{"name": "I", "valueType": "integer"}],
                        "data": [[2], [4]],
                    }
                ]
            ).encode(),
            "INDEX_INCONSISTENT",
        ),
    ],
)
def test_json_well_log_reports_exact_validation_failures(document: bytes, code: str) -> None:
    item = BytesFormatSource(file_id=uuid4(), content=document)
    with pytest.raises(FormatExtractionError, match=code):
        extract_json_well_log(ExtractJsonWellLogInput(file_id=item.file_id), item)


def test_dlis_and_lis_are_typed_bounded_and_wrap_library_failures() -> None:
    item = BytesFormatSource(file_id=uuid4(), content=b"native")
    origin = SimpleNamespace(id="origin", well_name="F-1")
    channel = SimpleNamespace(name="GR", units="API")
    frame = SimpleNamespace(name="FRAME", channels=[channel])
    logical = SimpleNamespace(
        fileheader=SimpleNamespace(id="LF-1"),
        frames=[frame],
        channels=[channel],
        origins=[origin],
    )

    dlis_result = extract_dlis(
        ExtractDlisInput(file_id=item.file_id),
        item,
        loader=lambda _: [logical],
        library_version="1.0-test",
    )
    assert dlis_result.output.logical_file_ids == ("LF-1",)
    assert dlis_result.output.frames[0].channel_count == 1
    assert dlis_result.output.channels[0].mnemonic == "GR"
    assert dlis_result.output.origins[0].well_name == "F-1"

    class LisLogical:
        def header(self) -> SimpleNamespace:
            return SimpleNamespace(file_name="LIS-1")

        def data_format_specs(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(specs=[SimpleNamespace(mnemonic="GR", units="API")])]

        def wellsite_data(self) -> list[SimpleNamespace]:
            return []

    lis_result = extract_lis(
        ExtractLisInput(file_id=item.file_id),
        item,
        loader=lambda _: [LisLogical()],
    )
    assert lis_result.output.record_count == 1
    assert lis_result.output.record_types == ("GR",)

    def broken(_: object) -> object:
        raise RuntimeError("native parser internal path C:\\secret\\well.dlis")

    with pytest.raises(FormatExtractionError, match="DLIS_OPEN_FAILED"):
        extract_dlis(
            ExtractDlisInput(
                file_id=item.file_id,
                options=DlisExtractionOptions(max_logical_files=1, max_channels=1),
            ),
            item,
            loader=broken,
            library_version="1.0-test",
        )

    class BrokenLogicalFiles:
        def __iter__(self) -> Iterator[object]:
            raise RuntimeError("native iteration failed")

    with pytest.raises(FormatExtractionError, match="DLIS_LIBRARY_ERROR"):
        extract_dlis(
            ExtractDlisInput(file_id=item.file_id),
            item,
            loader=lambda _: BrokenLogicalFiles(),
            library_version="1.0-test",
        )
    with pytest.raises(FormatExtractionError, match="LIS_RECORD_INVALID"):
        extract_lis(
            ExtractLisInput(
                file_id=item.file_id,
                options=LisExtractionOptions(max_records=1),
            ),
            item,
            loader=broken,
        )


def test_csv_p190_and_tool_013_independent_failure_codes() -> None:
    csv_bad = BytesFormatSource(file_id=uuid4(), content=b"\xff")
    with pytest.raises(FormatExtractionError, match="CSV_ENCODING_FAILED"):
        extract_csv(
            ExtractCsvInput(
                file_id=csv_bad.file_id,
                options=CsvExtractionOptions(encoding="utf-8"),
            ),
            csv_bad,
        )

    inconsistent = BytesFormatSource(file_id=uuid4(), content=b"a,b\n1\n")
    with pytest.raises(FormatExtractionError, match="CSV_ROW_INCONSISTENT"):
        extract_csv(ExtractCsvInput(file_id=inconsistent.file_id), inconsistent)

    p190 = BytesFormatSource(file_id=uuid4(), content=b"H0100 invalid\nS short\n")
    with pytest.raises(FormatExtractionError, match="P190_POSITION_INVALID"):
        extract_p190(ExtractP190Input(file_id=p190.file_id), p190)

    cases = (
        (InterpretationSubtype.SGP, b"not a grid", "SGP_INVALID"),
        (InterpretationSubtype.DAT, b"not interpretation", "DAT_PARSE_FAILED"),
        (InterpretationSubtype.TEXT, b"\xff", "TEXT_DECODE_FAILED"),
        (InterpretationSubtype.PDF, b"not pdf", "PDF_SIGNATURE_INVALID"),
    )
    for subtype, content, code in cases:
        item = BytesFormatSource(file_id=uuid4(), content=content)
        with pytest.raises(FormatExtractionError, match=code):
            extract_interpretation(
                ExtractInterpretationInput(file_id=item.file_id, subtype=subtype),
                item,
            )
