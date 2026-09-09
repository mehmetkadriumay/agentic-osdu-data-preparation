from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentic_osdu.domain.models import ApprovedAbsolutePath, WorkspaceRelativePath
from agentic_osdu.formats import BytesFormatSource, FormatExtractionError
from agentic_osdu.formats.interpretation import extract_interpretation
from agentic_osdu.formats.navigation import extract_p190
from agentic_osdu.formats.segy import extract_segy
from agentic_osdu.formats.supporting import extract_csv, extract_pdf
from agentic_osdu.formats.well_logs import (
    extract_dlis,
    extract_json_well_log,
    extract_las,
    extract_lis,
)
from agentic_osdu.tools.contracts import (
    DiscoverFilesInput,
    ExtractCsvInput,
    ExtractDlisInput,
    ExtractInterpretationInput,
    ExtractJsonWellLogInput,
    ExtractLasInput,
    ExtractLisInput,
    ExtractP190Input,
    ExtractSegyInput,
    InterpretationExtractionOptions,
    InterpretationSubtype,
    JsonWellLogOptions,
    RegisterWorkspaceInput,
    SegyExtractionOptions,
)
from agentic_osdu.tools.discovery import (
    DiscoveryError,
    DiscoveryService,
    InMemoryWorkspacePolicyStore,
)


def _source(content: bytes) -> BytesFormatSource:
    return BytesFormatSource(file_id=uuid4(), content=content)


def _variable_length_segy(sample_counts: tuple[int, ...]) -> bytes:
    binary = bytearray(400)
    struct.pack_into(">H", binary, 16, 2000)
    struct.pack_into(">H", binary, 20, sample_counts[0])
    struct.pack_into(">H", binary, 24, 5)
    struct.pack_into(">H", binary, 302, 0)
    traces = bytearray()
    for index, sample_count in enumerate(sample_counts):
        header = bytearray(240)
        struct.pack_into(">H", header, 114, sample_count)
        struct.pack_into(">i", header, 188, index + 1)
        traces.extend(header)
        traces.extend(b"\0" * (sample_count * 4))
    return b"C 1 TIME".ljust(3200) + binary + traces


def test_segy_honors_variable_length_flag_and_distinguishes_truncation() -> None:
    item = _source(_variable_length_segy((1, 3)))
    request = ExtractSegyInput(
        file_id=item.file_id,
        options=SegyExtractionOptions(max_trace_samples=1),
    )
    result = extract_segy(request, item)

    assert result.output.dimensions.trace_count == 2
    assert [trace.sample_count for trace in result.output.trace_samples] == [1]

    truncated = _source(_variable_length_segy((1, 3))[:-1])
    with pytest.raises(FormatExtractionError, match="SEGY_TRACE_TRUNCATED"):
        extract_segy(ExtractSegyInput(file_id=truncated.file_id), truncated)

    invalid_flag = bytearray(_variable_length_segy((1,)))
    struct.pack_into(">H", invalid_flag, 3502, 2)
    invalid = _source(bytes(invalid_flag))
    with pytest.raises(FormatExtractionError, match="SEGY_HEADER_INVALID"):
        extract_segy(ExtractSegyInput(file_id=invalid.file_id), invalid)


def test_json_well_log_streams_multiple_typed_log_sets_with_aggregate_bounds() -> None:
    document = [
        {
            "header": {"name": "main", "well": "F-1"},
            "curves": [{"name": "MD", "valueType": "integer"}],
            "data": [[1], [2]],
        },
        {
            "header": {
                "name": "repeat",
                "well": "F-2",
                "dataUri": "file:repeat-data.json",
            },
            "curves": [{"name": "TIME", "valueType": "float"}],
        },
    ]
    item = _source(json.dumps(document).encode())
    result = extract_json_well_log(
        ExtractJsonWellLogInput(
            file_id=item.file_id,
            options=JsonWellLogOptions(max_curves=2, max_rows=2),
        ),
        item,
    )

    assert result.output.well_name == "F-1"
    assert result.output.row_count == 2
    assert result.output.log_set_count == 2
    assert [log.name for log in result.output.log_sets] == ["main", "repeat"]
    assert result.output.log_sets[1].data_uri == "file:repeat-data.json"


@pytest.mark.parametrize(
    "log_set",
    [
        {"header": {"name": "missing"}, "curves": [{"name": "MD"}]},
        {"header": {"name": "empty"}, "curves": [{"name": "MD"}], "data": []},
        {
            "header": {"name": "unknown"},
            "curves": [{"name": "MD"}],
            "data": [[1]],
            "unexpected": True,
        },
    ],
)
def test_json_well_log_requires_nonempty_inline_data_or_valid_data_uri(
    log_set: dict[str, object],
) -> None:
    item = _source(json.dumps([log_set]).encode())
    with pytest.raises(FormatExtractionError, match="JSON_INVALID"):
        extract_json_well_log(ExtractJsonWellLogInput(file_id=item.file_id), item)


def test_p190_ports_datum_precedence_and_complete_line_summary_fields() -> None:
    headers = "\n".join(
        (
            "H1400" + (" " * 27) + "WGS 84",
            "H1500" + (" " * 27) + "ED50",
            "H1800" + (" " * 27) + "UTM",
            "H1900" + (" " * 27) + "31 NORTH",
            "H2600" + (" " * 27) + "Additional datum note",
        )
    )

    def position(point: int, easting: float, northing: float, depth: float) -> str:
        return (
            "S"
            + "LINE-A".ljust(12)
            + (" " * 6)
            + f"{point:06d}"
            + "600000.00N"
            + "0030000.00E"
            + f"{easting:9.1f}"
            + f"{northing:9.1f}"
            + f"{depth:6.1f}"
        )

    item = _source(
        f"{headers}\n{position(100, 500000, 6650000, 100)}\n"
        f"{position(102, 500010, 6650010, 110)}\n".encode()
    )
    output = extract_p190(ExtractP190Input(file_id=item.file_id), item).output

    assert output.inferred_epsg == 23031
    assert output.sampled_positions[0].easting == 500000
    assert output.sampled_positions[1].northing == 6650010
    assert output.sampled_positions[1].water_depth == 110
    summary = output.line_summaries[0]
    assert summary.point_increment == 2
    assert summary.easting_min == 500000
    assert summary.easting_max == 500010
    assert summary.northing_min == 6650000
    assert summary.northing_max == 6650010
    assert summary.water_depth_min == 100
    assert summary.water_depth_max == 110

    nzgd_headers = "\n".join(
        (
            "H1400" + (" " * 27) + "WGS 84",
            "H1800" + (" " * 27) + "NZTM",
            "H2600" + (" " * 27) + "Coordinates referenced to NZGD2000",
        )
    )
    nzgd = _source(f"{nzgd_headers}\n{position(1, 1, 1, 1)}\n".encode())
    assert extract_p190(ExtractP190Input(file_id=nzgd.file_id), nzgd).output.inferred_epsg == 2193


def test_lis_reads_bounded_wellsite_and_job_mnemonics_from_logical_file() -> None:
    item = _source(b"native")

    class LisLogical:
        def header(self) -> SimpleNamespace:
            return SimpleNamespace(well_name="wrong-header")

        def data_format_specs(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(specs=[SimpleNamespace(mnemonic="GR")])]

        def wellsite_data(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    components=lambda: [SimpleNamespace(mnemonic="WELL", component="wellsite-name")]
                )
            ]

        def job_identification(self) -> list[list[SimpleNamespace]]:
            return [[SimpleNamespace(mnemonic="WELL", value="job-name")]]

    result = extract_lis(
        ExtractLisInput(file_id=item.file_id),
        item,
        loader=lambda _: [LisLogical()],
    )
    assert result.output.well_name == "wellsite-name"


def test_sgp_and_dat_keep_complete_totals_when_samples_are_bounded() -> None:
    sgp = _source(b"H 1 1 1 1\n1 2\n3 4\n5 6\n")
    sgp_output = extract_interpretation(
        ExtractInterpretationInput(
            file_id=sgp.file_id,
            subtype=InterpretationSubtype.SGP,
            options=InterpretationExtractionOptions(max_rows=1),
        ),
        sgp,
    ).output.sgp
    assert sgp_output is not None
    assert sgp_output.row_count == 3
    assert sgp_output.sampled_row_count == 1
    assert sgp_output.truncated is True

    dat = _source(b"A\nB\nC\nD\n1,2,3,4,5\n2,3,4,5,6\n3,4,5,6,7\n")
    dat_output = extract_interpretation(
        ExtractInterpretationInput(
            file_id=dat.file_id,
            subtype=InterpretationSubtype.DAT,
            options=InterpretationExtractionOptions(max_rows=1),
        ),
        dat,
    ).output.dat
    assert dat_output is not None
    assert dat_output.point_count == 3
    assert dat_output.sampled_point_count == 1
    assert dat_output.truncated is True


def test_csv_accepts_registered_input_and_validates_source_identity() -> None:
    item = _source(b"a,b\n1,2\n")
    result = extract_csv(ExtractCsvInput(file_id=item.file_id), item)
    assert result.output.headers == ("a", "b")

    with pytest.raises(FormatExtractionError, match="FILE_CHANGED"):
        extract_csv(ExtractCsvInput(file_id=uuid4()), item)


def test_native_parser_cannot_follow_replacement_after_discovery(tmp_path: Path) -> None:
    root = tmp_path
    path = root / "source.dlis"
    path.write_bytes(b"approved")
    service = DiscoveryService(store=InMemoryWorkspacePolicyStore())
    workspace = service.register_workspace(
        RegisterWorkspaceInput(root_path=ApprovedAbsolutePath(str(root)), read_only=True)
    )
    discovered = service.discover_files(DiscoverFilesInput(workspace_id=workspace.workspace_id))
    source = service.format_source(discovered.files[0].file_id)
    observed: list[bytes] = []
    replacement_blocked = False

    def replacing_loader(locator: object) -> object:
        nonlocal replacement_blocked
        replacement = root / "replacement.dlis"
        replacement.write_bytes(b"unapproved")
        try:
            os.replace(replacement, path)
        except PermissionError:
            replacement_blocked = True
        if hasattr(locator, "read"):
            observed.append(locator.read())
        else:
            with open(str(locator), "rb") as stream:
                observed.append(stream.read())
        return [
            SimpleNamespace(
                fileheader=SimpleNamespace(id="LF"),
                frames=[],
                channels=[],
                origins=[],
            )
        ]

    if os.name == "nt":
        result = extract_dlis(
            ExtractDlisInput(file_id=source.file_id),
            source,
            loader=replacing_loader,
            library_version="test",
        )
        assert replacement_blocked is True
        assert result.output.logical_file_ids == ("LF",)
    else:
        with pytest.raises(DiscoveryError, match="FILE_CHANGED"):
            extract_dlis(
                ExtractDlisInput(file_id=source.file_id),
                source,
                loader=replacing_loader,
                library_version="test",
            )
        assert replacement_blocked is False
    assert observed == [b"approved"]


def test_json_well_log_uses_header_data_uri_and_rejects_scalar_log_sets() -> None:
    external = _source(
        json.dumps(
            [
                {
                    "header": {
                        "name": "external",
                        "well": "F-1",
                        "dataUri": "file:external.json",
                    },
                    "curves": [{"name": "MD", "valueType": "float"}],
                    "data": [],
                }
            ]
        ).encode()
    )
    output = extract_json_well_log(
        ExtractJsonWellLogInput(file_id=external.file_id),
        external,
    ).output
    assert output.log_sets[0].data_uri == "file:external.json"

    malformed = _source(
        json.dumps(
            [
                123,
                {
                    "header": {"name": "valid"},
                    "curves": [{"name": "MD"}],
                    "data": [[1]],
                },
            ]
        ).encode()
    )
    with pytest.raises(FormatExtractionError, match="JSON_INVALID"):
        extract_json_well_log(
            ExtractJsonWellLogInput(file_id=malformed.file_id),
            malformed,
        )


def test_dat_companion_capability_must_match_requested_relative_path() -> None:
    dat = _source(b"A\nB\nC\nD\n1,2,3,4,5\n")
    companion = BytesFormatSource(
        file_id=uuid4(),
        content=b"Type: UTM\nZone: 31 NORTH\n",
        relative_path=WorkspaceRelativePath("actual.crs"),
    )
    request = ExtractInterpretationInput(
        file_id=dat.file_id,
        subtype=InterpretationSubtype.DAT,
        options=InterpretationExtractionOptions(
            companion_crs_path=WorkspaceRelativePath("expected.crs")
        ),
    )
    with pytest.raises(FormatExtractionError, match="COMPANION_CRS_INVALID"):
        extract_interpretation(request, dat, companion_source=companion)


def test_p190_rejects_out_of_range_dms_with_structured_error() -> None:
    position = (
        "S"
        + "LINE-A".ljust(12)
        + (" " * 6)
        + "000001"
        + "996000.00N"
        + "0030000.00E"
        + "500000.0"
        + "6650000.0"
        + "00100."
    )
    item = _source(f"{position}\n".encode())
    with pytest.raises(FormatExtractionError, match="P190_POSITION_INVALID"):
        extract_p190(ExtractP190Input(file_id=item.file_id), item)


def test_las_oversized_curve_field_uses_structured_error() -> None:
    item = _source(
        (
            "~Version\nVERS. 2.0 : version\n~Curve\n" + ("X" * 65) + ".M : invalid\n~ASCII\n1\n"
        ).encode()
    )
    with pytest.raises(FormatExtractionError, match="LAS_SECTION_INVALID"):
        extract_las(ExtractLasInput(file_id=item.file_id), item)


@pytest.mark.parametrize("prefix", [b"%PDF-\n", b"%PDF-garbage\n", b"%PDF-1.8\n"])
def test_pdf_rejects_invalid_version_markers(prefix: bytes) -> None:
    item = _source(prefix)
    with pytest.raises(FormatExtractionError, match="PDF_SIGNATURE_INVALID"):
        extract_pdf(item)
