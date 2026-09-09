"""TOOL-013 explicit SGP, DAT, text, and PDF subtype adapters."""

from __future__ import annotations

import re
from io import TextIOWrapper

from agentic_osdu.domain.models import DataDomain, EvidenceRecord, WorkspaceRelativePath
from agentic_osdu.formats import (
    CancellationCheck,
    ExtractionOutcome,
    FormatExtractionError,
    FormatSource,
    check_cancelled,
    evidence,
    validate_source,
)
from agentic_osdu.formats.supporting import extract_pdf, extract_text
from agentic_osdu.tools.contracts import (
    DatMetadata,
    ExtractInterpretationInput,
    InterpretationMetadataOutput,
    InterpretationSubtype,
    SgpMetadata,
)


def _extract_sgp(
    source: FormatSource,
    *,
    max_rows: int,
    cancellation: CancellationCheck | None,
) -> ExtractionOutcome[SgpMetadata]:
    corners = 0
    grid_rows = 0
    column_count = 0
    _line_count = 0
    try:
        with (
            source.open_binary() as binary,
            TextIOWrapper(binary, encoding="ascii", errors="strict") as text,
        ):
            for _line_count, raw_line in enumerate(text, start=1):
                check_cancelled(cancellation)
                line = raw_line.strip()
                if not line:
                    continue
                if re.fullmatch(
                    r"H\s+[-+]?\d+\s+[-+]?\d+\s+[-+]?\d+(?:\.\d+)?\s+[-+]?\d+(?:\.\d+)?",
                    line,
                ):
                    corners += 1
                    continue
                values = line.split()
                try:
                    [float(value) for value in values]
                except ValueError:
                    continue
                grid_rows += 1
                if column_count == 0:
                    column_count = len(values)
                elif len(values) != column_count:
                    raise FormatExtractionError(
                        "SGP_INVALID", "SGP grid rows have inconsistent column counts."
                    )
    except UnicodeDecodeError as error:
        raise FormatExtractionError("SGP_INVALID", "SGP content is not valid ASCII.") from error
    if corners == 0 or grid_rows == 0 or column_count == 0:
        raise FormatExtractionError(
            "SGP_INVALID", "SGP corner declarations and numeric grid rows are required."
        )
    return ExtractionOutcome(
        output=SgpMetadata(
            row_count=grid_rows,
            column_count=column_count,
            domain=DataDomain.UNKNOWN,
            sampled_row_count=min(grid_rows, max_rows),
            truncated=grid_rows > max_rows,
        ),
        evidence=(
            evidence(
                source.file_id,
                "SGP-1.GRID",
                "SGP corner declarations and bounded numeric rows were parsed.",
                location=f"lines 1-{_line_count}",
                observed_value=f"{grid_rows}x{column_count}",
            ),
        ),
    )


def _companion_crs(
    source: FormatSource | None,
    encoding: str,
    expected_path: WorkspaceRelativePath | None,
) -> tuple[str | None, tuple[EvidenceRecord, ...]]:
    if source is None:
        return None, ()
    if expected_path is None or source.relative_path != expected_path:
        raise FormatExtractionError(
            "COMPANION_CRS_INVALID",
            "The approved companion capability does not match the requested relative path.",
        )
    try:
        with source.open_binary(max_bytes=64 * 1024) as stream:
            text = stream.read().decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError) as error:
        raise FormatExtractionError(
            "COMPANION_CRS_INVALID", "The approved companion CRS text cannot be decoded."
        ) from error
    projection = re.search(r"type\s*:\s*([^\r\n]+)", text, re.IGNORECASE)
    zone = re.search(r"zone\s*:\s*([^\r\n]+)", text, re.IGNORECASE)
    values = [match.group(1).strip() for match in (projection, zone) if match]
    if not values:
        raise FormatExtractionError(
            "COMPANION_CRS_INVALID", "The approved companion does not declare CRS fields."
        )
    value = " / ".join(values)
    return (
        value,
        (
            evidence(
                source.file_id,
                "DAT-2.COMPANION_CRS",
                "CRS fields were parsed from the approved companion capability.",
                location=f"{expected_path.root}:bytes 0-65535",
                observed_value=value,
            ),
        ),
    )


def _extract_dat(
    source: FormatSource,
    *,
    max_rows: int,
    encoding: str,
    companion: FormatSource | None,
    companion_path: WorkspaceRelativePath | None,
    cancellation: CancellationCheck | None,
) -> ExtractionOutcome[DatMetadata]:
    metadata: list[str] = []
    crs: str | None = None
    horizon_points = 0
    fault_points = 0
    _line_count = 0
    try:
        with (
            source.open_binary() as binary,
            TextIOWrapper(binary, encoding=encoding, errors="strict") as text,
        ):
            for _line_count, raw_line in enumerate(text, start=1):
                check_cancelled(cancellation)
                line = raw_line.strip()
                if not line or line == "=":
                    continue
                if line.startswith("#"):
                    if crs is None and "cartographic system name" in line.casefold():
                        crs = line.split(":", 1)[-1].strip() or None
                    continue
                fields = line.split(",")
                if len(fields) == 5:
                    try:
                        [float(field) for field in fields]
                    except ValueError:
                        if horizon_points == 0 and len(metadata) < 4:
                            metadata.append(line)
                    else:
                        horizon_points += 1
                    continue
                if horizon_points == 0 and len(metadata) < 4:
                    metadata.append(line)
                if len(raw_line) < 100:
                    continue
                try:
                    float(raw_line[2:12])
                    float(raw_line[13:24])
                    float(raw_line[25:36])
                except ValueError:
                    continue
                if raw_line[52:100].strip():
                    fault_points += 1
    except (LookupError, UnicodeDecodeError) as error:
        raise FormatExtractionError(
            "DAT_PARSE_FAILED", "DAT content cannot be decoded with the selected encoding."
        ) from error
    if horizon_points and len(metadata) >= 4:
        companion_evidence: tuple[EvidenceRecord, ...] = ()
        if crs is None:
            crs, companion_evidence = _companion_crs(companion, encoding, companion_path)
        return ExtractionOutcome(
            output=DatMetadata(
                interpretation_type="horizon",
                point_count=horizon_points,
                crs=crs,
                sampled_point_count=min(horizon_points, max_rows),
                truncated=horizon_points > max_rows,
            ),
            evidence=(
                evidence(
                    source.file_id,
                    "DAT-1.HORIZON",
                    "OpenWorks metadata and five-column horizon points were parsed.",
                    location=f"lines 1-{_line_count}",
                    observed_value=str(horizon_points),
                ),
                *companion_evidence,
            ),
        )
    if not fault_points:
        raise FormatExtractionError(
            "DAT_PARSE_FAILED", "No supported horizon or fixed-width fault records were found."
        )
    crs, companion_evidence = _companion_crs(companion, encoding, companion_path)
    return ExtractionOutcome(
        output=DatMetadata(
            interpretation_type="fault",
            point_count=fault_points,
            crs=crs,
            sampled_point_count=min(fault_points, max_rows),
            truncated=fault_points > max_rows,
        ),
        evidence=(
            evidence(
                source.file_id,
                "DAT-1.FAULT",
                "Fixed-width XYZ fault-stick records were parsed.",
                location=f"lines 1-{_line_count}",
                observed_value=str(fault_points),
            ),
            *companion_evidence,
        ),
    )


def extract_interpretation(
    request: ExtractInterpretationInput,
    source: FormatSource,
    *,
    companion_source: FormatSource | None = None,
    cancellation: CancellationCheck | None = None,
) -> ExtractionOutcome[InterpretationMetadataOutput]:
    """Dispatch TOOL-013 only through the requested explicit subtype adapter."""

    validate_source(request.file_id, source)
    if request.options.companion_crs_path is not None and companion_source is None:
        raise FormatExtractionError(
            "COMPANION_CRS_INVALID", "The approved companion CRS capability is unavailable."
        )
    if request.subtype is InterpretationSubtype.SGP:
        sgp_result = _extract_sgp(
            source,
            max_rows=request.options.max_rows,
            cancellation=cancellation,
        )
        output = InterpretationMetadataOutput(subtype=request.subtype, sgp=sgp_result.output)
        result_evidence = sgp_result.evidence
    elif request.subtype is InterpretationSubtype.DAT:
        dat_result = _extract_dat(
            source,
            max_rows=request.options.max_rows,
            encoding=request.options.text_encoding or "latin-1",
            companion=companion_source,
            companion_path=request.options.companion_crs_path,
            cancellation=cancellation,
        )
        output = InterpretationMetadataOutput(subtype=request.subtype, dat=dat_result.output)
        result_evidence = dat_result.evidence
    elif request.subtype is InterpretationSubtype.TEXT:
        text_result = extract_text(
            source,
            max_lines=request.options.max_rows,
            requested_encoding=request.options.text_encoding,
            cancellation=cancellation,
        )
        output = InterpretationMetadataOutput(subtype=request.subtype, text=text_result.output)
        result_evidence = text_result.evidence
    else:
        pdf_result = extract_pdf(source)
        output = InterpretationMetadataOutput(subtype=request.subtype, pdf=pdf_result.output)
        result_evidence = pdf_result.evidence
    return ExtractionOutcome(output=output, evidence=result_evidence)


__all__ = ["extract_interpretation"]
