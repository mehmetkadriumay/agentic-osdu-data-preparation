"""TOOL-011 CSV and TOOL-013 supporting-document extractors."""

from __future__ import annotations

import csv
import re
from io import TextIOWrapper

from agentic_osdu.formats import (
    CancellationCheck,
    ExtractionOutcome,
    FormatExtractionError,
    FormatSource,
    check_cancelled,
    evidence,
    validate_source,
)
from agentic_osdu.tools.contracts import (
    CsvMetadata,
    CsvRowSample,
    ExtractCsvInput,
    PdfMetadata,
    TextMetadata,
)


def _encoding(source: FormatSource, requested: str | None, code: str) -> str:
    if requested is not None:
        return requested
    with source.open_binary(max_bytes=4096) as stream:
        prefix = stream.read()
    if prefix.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        prefix.decode("ascii")
        return "ascii"
    except UnicodeDecodeError:
        try:
            prefix.decode("utf-8")
        except UnicodeDecodeError:
            if (
                sum(byte in (9, 10, 13) or 32 <= byte <= 126 for byte in prefix)
                / max(1, len(prefix))
                < 0.85
            ):
                raise FormatExtractionError(
                    code, "The source text encoding cannot be decoded safely."
                ) from None
            return "latin-1"
        return "utf-8"


def extract_csv(
    request: ExtractCsvInput,
    source: FormatSource,
    *,
    cancellation: CancellationCheck | None = None,
) -> ExtractionOutcome[CsvMetadata]:
    """Infer a CSV dialect and retain only the configured representative rows."""

    validate_source(request.file_id, source)
    options = request.options
    encoding = _encoding(source, options.encoding, "CSV_ENCODING_FAILED")
    try:
        with (
            source.open_binary() as binary,
            TextIOWrapper(binary, encoding=encoding, errors="strict", newline="") as text,
        ):
            first_line = text.readline()
            if not first_line:
                raise FormatExtractionError(
                    "CSV_DIALECT_UNKNOWN", "The CSV source has no header row."
                )
            delimiter = options.delimiter_hint
            if delimiter is None:
                try:
                    delimiter = csv.Sniffer().sniff(first_line, delimiters=",;\t|").delimiter
                except csv.Error as error:
                    raise FormatExtractionError(
                        "CSV_DIALECT_UNKNOWN",
                        "A supported single-character CSV delimiter was not detected.",
                    ) from error
            headers = tuple(next(csv.reader([first_line], delimiter=delimiter)))
            if not headers:
                raise FormatExtractionError("CSV_DIALECT_UNKNOWN", "The CSV header row is empty.")
            samples: list[CsvRowSample] = []
            reader = csv.reader(text, delimiter=delimiter)
            for row_number, row in enumerate(reader, start=2):
                check_cancelled(cancellation)
                if len(row) != len(headers):
                    raise FormatExtractionError(
                        "CSV_ROW_INCONSISTENT",
                        "A sampled CSV row does not match the header width.",
                    )
                if len(samples) < options.max_sample_rows:
                    samples.append(CsvRowSample(row_number=row_number, values=tuple(row)))
                else:
                    break
    except FormatExtractionError:
        raise
    except (LookupError, UnicodeDecodeError) as error:
        raise FormatExtractionError(
            "CSV_ENCODING_FAILED", "The CSV source cannot be decoded with the selected encoding."
        ) from error
    output = CsvMetadata(
        delimiter=delimiter,
        headers=headers,
        sampled_row_count=len(samples),
        column_count=len(headers),
        rows_consistent=True,
        sampled_rows=tuple(samples),
    )
    return ExtractionOutcome(
        output=output,
        evidence=(
            evidence(
                source.file_id,
                "CSV-1.DIALECT",
                "Delimiter and header fields were parsed from the first record.",
                location="row 1",
                observed_value=delimiter,
            ),
            evidence(
                source.file_id,
                "CSV-1.ROWS",
                "Representative rows were checked against the header width.",
                location=f"rows 2-{len(samples) + 1}",
                observed_value=str(len(samples)),
            ),
        ),
    )


def extract_text(
    source: FormatSource,
    *,
    max_lines: int,
    requested_encoding: str | None,
    cancellation: CancellationCheck | None = None,
) -> ExtractionOutcome[TextMetadata]:
    encoding = _encoding(source, requested_encoding, "TEXT_DECODE_FAILED")
    line_count = 0
    truncated = False
    try:
        with (
            source.open_binary() as binary,
            TextIOWrapper(binary, encoding=encoding, errors="strict") as text,
        ):
            for _line in text:
                check_cancelled(cancellation)
                if line_count >= max_lines:
                    truncated = True
                    break
                line_count += 1
    except (LookupError, UnicodeDecodeError) as error:
        raise FormatExtractionError(
            "TEXT_DECODE_FAILED", "The text source cannot be decoded with the selected encoding."
        ) from error
    return ExtractionOutcome(
        output=TextMetadata(encoding=encoding, line_count=line_count, truncated=truncated),
        evidence=(
            evidence(
                source.file_id,
                "TEXT-1.LINES",
                "Text lines were counted within the configured bound.",
                location=f"lines 1-{line_count}",
                observed_value=encoding,
            ),
        ),
    )


def extract_pdf(source: FormatSource) -> ExtractionOutcome[PdfMetadata]:
    with source.open_binary(max_bytes=16) as stream:
        prefix = stream.read(16)
    match = re.match(rb"%PDF-((?:1\.[0-7])|(?:2\.0))(?:\r\n|\r|\n)", prefix)
    if match is None:
        raise FormatExtractionError(
            "PDF_SIGNATURE_INVALID", "The PDF signature or version marker is invalid."
        )
    version = match.group(1).decode("ascii")
    return ExtractionOutcome(
        output=PdfMetadata(signature_valid=True, version=version, size_bytes=source.size_bytes),
        evidence=(
            evidence(
                source.file_id,
                "PDF-1.SIGNATURE",
                "The PDF signature and version marker were parsed.",
                location="bytes 0-7",
                observed_value=version,
            ),
        ),
    )


__all__ = ["extract_csv", "extract_pdf", "extract_text"]
