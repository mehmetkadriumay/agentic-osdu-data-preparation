from __future__ import annotations

import json
import struct
from uuid import UUID, uuid4

from agentic_osdu.domain.models import DataDomain
from agentic_osdu.formats import BytesFormatSource
from agentic_osdu.formats.interpretation import extract_interpretation
from agentic_osdu.formats.navigation import extract_p190
from agentic_osdu.formats.segy import extract_segy
from agentic_osdu.formats.supporting import extract_csv
from agentic_osdu.formats.well_logs import extract_json_well_log, extract_las
from agentic_osdu.tools.contracts import (
    CsvExtractionOptions,
    ExtractCsvInput,
    ExtractInterpretationInput,
    ExtractJsonWellLogInput,
    ExtractLasInput,
    ExtractP190Input,
    ExtractSegyInput,
    InterpretationSubtype,
)


def source(data: bytes, *, file_id: UUID | None = None) -> BytesFormatSource:
    return BytesFormatSource(file_id=file_id or uuid4(), content=data)


def segy_bytes() -> bytes:
    textual = b"C 1 SURVEY: VOLVE 3D DEPTH".ljust(3200, b" ")
    binary = bytearray(400)
    struct.pack_into(">H", binary, 16, 2000)
    struct.pack_into(">H", binary, 20, 2)
    struct.pack_into(">H", binary, 24, 5)
    struct.pack_into(">H", binary, 54, 1)
    trace = bytearray(248)
    struct.pack_into(">i", trace, 0, 1)
    struct.pack_into(">H", trace, 114, 2)
    struct.pack_into(">H", trace, 116, 2000)
    struct.pack_into(">i", trace, 188, 100)
    struct.pack_into(">i", trace, 192, 200)
    return textual + binary + trace


def test_item_017_extracts_segy_headers_endian_dimensions_and_bounded_traces() -> None:
    data = segy_bytes()
    item = source(data)

    outcome = extract_segy(
        ExtractSegyInput(file_id=item.file_id),
        item,
    )

    assert outcome.output.endian == "big"
    assert outcome.output.textual_header_encoding == "ascii"
    assert outcome.output.sample_interval_microseconds == 2000
    assert outcome.output.samples_per_trace == 2
    assert outcome.output.sampled_trace_count == 1
    assert outcome.output.dimensions.inline_count == 1
    assert outcome.output.dimensions.crossline_count == 1
    assert outcome.output.dimensions.trace_count == 1
    assert outcome.output.domain is DataDomain.DEPTH
    assert outcome.output.trace_samples[0].byte_offset == 3600
    assert {evidence.location for evidence in outcome.evidence} >= {
        "bytes 3200-3599",
        "bytes 3600-3839",
    }


def test_items_018_019_extract_las_and_stream_json_well_log() -> None:
    las = source(
        b"""~Version Information
VERS. 2.0 : version
~Well Information
WELL. : Volve F-1
~Curve Information
DEPT.M : Measured depth
GR.API : Gamma ray
~ASCII
1000 10
1001 11
"""
    )
    las_result = extract_las(ExtractLasInput(file_id=las.file_id), las)
    assert las_result.output.version == "2.0"
    assert las_result.output.well_name == "Volve F-1"
    assert las_result.output.sections == ("VERSION", "WELL", "CURVE", "ASCII")
    assert [curve.mnemonic for curve in las_result.output.curves] == ["DEPT", "GR"]
    assert las_result.output.row_count == 2

    document = [
        {
            "header": {
                "name": "Main",
                "well": "Volve F-1",
                "startIndex": 1000,
                "endIndex": 1001,
                "step": 1,
            },
            "curves": [
                {"name": "DEPT", "unit": "m", "valueType": "float"},
                {"name": "GR", "unit": "API", "valueType": "float"},
            ],
            "data": [[1000, 10.5], [1001, 11.5]],
        }
    ]
    well_log = source(json.dumps(document).encode())
    result = extract_json_well_log(
        ExtractJsonWellLogInput(file_id=well_log.file_id),
        well_log,
    )
    assert result.output.well_name == "Volve F-1"
    assert result.output.index_curve == "DEPT"
    assert result.output.row_count == 2
    assert result.output.column_count == 2
    assert [curve.value_type for curve in result.output.curves] == ["float", "float"]


def test_items_021_022_extract_csv_and_p190_navigation() -> None:
    csv_source = source(b"MD;GR\n1000;10\n1001;11\n")
    csv_result = extract_csv(
        ExtractCsvInput(
            file_id=csv_source.file_id,
            options=CsvExtractionOptions(max_sample_rows=1),
        ),
        csv_source,
    )
    assert csv_result.output.delimiter == ";"
    assert csv_result.output.headers == ("MD", "GR")
    assert csv_result.output.sampled_row_count == 1
    assert csv_result.output.column_count == 2
    assert csv_result.output.rows_consistent is True

    header = "H1800" + (" " * 27) + "U.T.M"
    datum = "H1400" + (" " * 27) + "WGS 84"
    zone = "H1900" + (" " * 27) + "31 NORTH"
    position = (
        "S"
        + "VOLVE-LINE".ljust(12)
        + (" " * 6)
        + "000001"
        + "600000.00N"
        + "0030000.00E"
        + "500000.0"
        + "6650000.0"
        + "00100."
    )
    navigation = source(f"{header}\n{datum}\n{zone}\n{position}\n".encode("ascii"))
    result = extract_p190(ExtractP190Input(file_id=navigation.file_id), navigation)
    assert result.output.line_names == ("VOLVE-LINE",)
    assert result.output.position_count == 1
    assert result.output.sampled_positions[0].latitude == 60.0
    assert result.output.sampled_positions[0].longitude == 3.0
    assert result.output.inferred_epsg == 32631
    assert result.output.headers[0].line_number == 1


def test_item_023_extracts_each_explicit_tool_013_subtype() -> None:
    sgp = source(b"H 1 1 100.0 200.0\nH 1 2 101.0 200.0\n1 2\n3 4\n")
    sgp_result = extract_interpretation(
        ExtractInterpretationInput(file_id=sgp.file_id, subtype=InterpretationSubtype.SGP),
        sgp,
    )
    assert sgp_result.output.sgp is not None
    assert sgp_result.output.sgp.row_count == 2
    assert sgp_result.output.sgp.column_count == 2

    dat = source(
        b"""# Source cartographic system name: EPSG:32631
VOLVE
HUGIN
INTERPRETER
DEPTH
1,2,100.0,200.0,300.0
"""
    )
    dat_result = extract_interpretation(
        ExtractInterpretationInput(file_id=dat.file_id, subtype=InterpretationSubtype.DAT),
        dat,
    )
    assert dat_result.output.dat is not None
    assert dat_result.output.dat.interpretation_type == "horizon"
    assert dat_result.output.dat.point_count == 1
    assert dat_result.output.dat.crs == "EPSG:32631"

    text = source(b"first\nsecond\n")
    text_result = extract_interpretation(
        ExtractInterpretationInput(file_id=text.file_id, subtype=InterpretationSubtype.TEXT),
        text,
    )
    assert text_result.output.text is not None
    assert text_result.output.text.encoding == "ascii"
    assert text_result.output.text.line_count == 2

    pdf = source(b"%PDF-1.7\nminimal")
    pdf_result = extract_interpretation(
        ExtractInterpretationInput(file_id=pdf.file_id, subtype=InterpretationSubtype.PDF),
        pdf,
    )
    assert pdf_result.output.pdf is not None
    assert pdf_result.output.pdf.signature_valid is True
    assert pdf_result.output.pdf.version == "1.7"
    assert pdf_result.output.pdf.size_bytes == len(b"%PDF-1.7\nminimal")
