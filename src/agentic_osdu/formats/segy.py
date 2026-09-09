"""TOOL-006 deterministic bounded SEG-Y metadata extraction."""

from __future__ import annotations

import struct
from collections import Counter
from typing import cast

from agentic_osdu.domain.models import (
    ClassificationDimensions,
    DataDomain,
    TrustLevel,
)
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
    ExtractSegyInput,
    SegyBinaryHeader,
    SegyMetadata,
    SegySurveyMetadata,
    SegyTraceSample,
)

_SAMPLE_WIDTHS = {1: 4, 2: 4, 3: 2, 5: 4, 6: 8, 7: 3, 8: 1, 9: 8, 10: 4, 11: 2, 12: 8, 15: 3, 16: 1}


def _uint16(raw: bytes, offset: int, endian: str) -> int:
    return cast(int, struct.unpack_from(f"{endian}H", raw, offset)[0])


def _int16(raw: bytes, offset: int, endian: str) -> int:
    return cast(int, struct.unpack_from(f"{endian}h", raw, offset)[0])


def _int32(raw: bytes, offset: int, endian: str) -> int:
    return cast(int, struct.unpack_from(f"{endian}i", raw, offset)[0])


def _endian_score(binary: bytes, endian: str) -> int:
    interval = _uint16(binary, 16, endian)
    samples = _uint16(binary, 20, endian)
    sample_format = _uint16(binary, 24, endian)
    return (
        (3 if 50 <= interval <= 16_000 else 0)
        + (3 if 1 <= samples <= 65_535 else 0)
        + (4 if sample_format in _SAMPLE_WIDTHS else 0)
    )


def _text_header(raw: bytes) -> tuple[str, str]:
    try:
        return raw.decode("ascii"), "ascii"
    except UnicodeDecodeError:
        try:
            return raw.decode("cp500"), "cp500"
        except UnicodeDecodeError as error:
            raise FormatExtractionError(
                "SEGY_HEADER_INVALID", "The textual header encoding is invalid."
            ) from error


def extract_segy(
    request: ExtractSegyInput,
    source: FormatSource,
    *,
    cancellation: CancellationCheck | None = None,
) -> ExtractionOutcome[SegyMetadata]:
    """Parse headers and bounded trace samples from an approved source capability."""

    validate_source(request.file_id, source)
    check_cancelled(cancellation)
    with source.open_binary(max_bytes=3600) as stream:
        header = stream.read(3600)
    if len(header) < 3600:
        raise FormatExtractionError(
            "SEGY_HEADER_INVALID", "The required 3600-byte SEG-Y header is incomplete."
        )
    text, text_encoding = _text_header(header[:3200])
    binary = header[3200:3600]
    scores = {"big": _endian_score(binary, ">"), "little": _endian_score(binary, "<")}
    if request.options.endian_hint is not None:
        endian_name = request.options.endian_hint
    elif scores["big"] == scores["little"]:
        raise FormatExtractionError(
            "SEGY_ENDIAN_AMBIGUOUS",
            "Binary-header byte order cannot be selected deterministically.",
        )
    else:
        endian_name = max(scores, key=scores.__getitem__)
    endian = ">" if endian_name == "big" else "<"
    interval = _uint16(binary, 16, endian)
    samples = _uint16(binary, 20, endian)
    sample_format = _uint16(binary, 24, endian)
    width = _SAMPLE_WIDTHS.get(sample_format)
    if interval == 0 or samples == 0 or width is None:
        raise FormatExtractionError(
            "SEGY_HEADER_INVALID", "Binary-header trace sizing fields are invalid."
        )
    extended_headers = _uint16(binary, 304, endian)
    trace_offset = 3600 + (extended_headers * 3200 if extended_headers < 1000 else 0)
    available = source.size_bytes - trace_offset
    if available < 0:
        raise FormatExtractionError(
            "SEGY_TRACE_TRUNCATED", "Trace data begins beyond the available source bytes."
        )
    fixed_length_flag = _uint16(binary, 302, endian)
    if fixed_length_flag not in {0, 1}:
        raise FormatExtractionError(
            "SEGY_HEADER_INVALID", "The fixed-length trace flag is invalid."
        )
    fixed_length = fixed_length_flag == 1
    traces: list[SegyTraceSample] = []
    inline_values: set[int] = set()
    crossline_values: set[int] = set()
    bin_counts: Counter[tuple[int, int]] = Counter()

    def retain_trace(trace_index: int, offset: int, trace: bytes) -> None:
        trace_samples = _uint16(trace, 114, endian)
        inline = _int32(trace, 188, endian)
        crossline = _int32(trace, 192, endian)
        if inline:
            inline_values.add(inline)
        if crossline:
            crossline_values.add(crossline)
        if inline or crossline:
            bin_counts[(inline, crossline)] += 1
        traces.append(
            SegyTraceSample(
                trace_index=trace_index,
                byte_offset=offset,
                sample_count=trace_samples,
                inline_number=inline or None,
                crossline_number=crossline or None,
            )
        )

    trace_count = 0
    with source.open_binary() as stream:
        if fixed_length:
            trace_size = 240 + samples * width
            if available % trace_size:
                raise FormatExtractionError(
                    "SEGY_TRACE_TRUNCATED",
                    "Trace bytes do not form complete fixed-length records.",
                )
            trace_count = available // trace_size
            sample_count = min(trace_count, request.options.max_trace_samples)
            sample_indices = (
                sorted(
                    {
                        round(index * (trace_count - 1) / max(1, sample_count - 1))
                        for index in range(sample_count)
                    }
                )
                if sample_count
                else []
            )
            for trace_index in sample_indices:
                check_cancelled(cancellation)
                offset = trace_offset + trace_index * trace_size
                stream.seek(offset)
                trace = stream.read(240)
                if len(trace) < 240:
                    raise FormatExtractionError(
                        "SEGY_TRACE_TRUNCATED", "A sampled trace header is incomplete."
                    )
                retain_trace(trace_index, offset, trace)
        else:
            offset = trace_offset
            stream.seek(offset)
            while offset < source.size_bytes:
                check_cancelled(cancellation)
                trace = stream.read(240)
                if len(trace) < 240:
                    raise FormatExtractionError(
                        "SEGY_TRACE_TRUNCATED", "A variable-length trace header is incomplete."
                    )
                trace_samples = _uint16(trace, 114, endian)
                if trace_samples == 0:
                    raise FormatExtractionError(
                        "SEGY_HEADER_INVALID",
                        "A variable-length trace declares an invalid sample count.",
                    )
                data_size = trace_samples * width
                trace_end = offset + 240 + data_size
                if trace_end > source.size_bytes or len(stream.read(data_size)) != data_size:
                    raise FormatExtractionError(
                        "SEGY_TRACE_TRUNCATED", "A variable-length trace payload is incomplete."
                    )
                if len(traces) < request.options.max_trace_samples:
                    retain_trace(trace_count, offset, trace)
                trace_count += 1
                offset = trace_end
    upper_text = text.upper()
    domain = DataDomain.DEPTH if "DEPTH" in upper_text else DataDomain.TIME
    survey_name = None
    for line in text.splitlines():
        if "SURVEY:" in line.upper():
            survey_name = line.split(":", 1)[1].strip()[:256] or None
            break
    output = SegyMetadata(
        endian=endian_name,
        textual_header_encoding=text_encoding if request.options.inspect_textual_header else None,
        sample_interval_microseconds=interval,
        samples_per_trace=samples,
        sampled_trace_count=len(traces),
        domain=domain,
        dimensions=ClassificationDimensions(
            inline_count=len(inline_values),
            crossline_count=len(crossline_values),
            trace_count=trace_count,
            sample_count=samples,
        ),
        binary_header=SegyBinaryHeader(
            job_id=_int32(binary, 0, endian),
            line_number=_int32(binary, 4, endian),
            reel_number=_int32(binary, 8, endian),
            data_traces_per_ensemble=_uint16(binary, 12, endian),
            auxiliary_traces_per_ensemble=_uint16(binary, 14, endian),
            sample_format_code=sample_format,
            fixed_length_trace_flag=1 if fixed_length else 0,
        ),
        survey_metadata=SegySurveyMetadata(
            survey_name=survey_name,
            measurement_system={1: "metres", 2: "feet"}.get(_uint16(binary, 54, endian)),
        ),
        trace_samples=tuple(traces),
    )
    return ExtractionOutcome(
        output=output,
        evidence=(
            evidence(
                source.file_id,
                "SEGY-1.BINARY-HEADER",
                "SEG-Y binary-header fields were parsed at fixed offsets.",
                location="bytes 3200-3599",
                observed_value=endian_name,
                trust=TrustLevel.HEURISTIC,
            ),
            evidence(
                source.file_id,
                "SEGY-1.TRACE-SAMPLE",
                "Trace headers were sampled within the configured bound.",
                location="bytes 3600-3839",
                observed_value=str(len(traces)),
            ),
        ),
    )


__all__ = ["extract_segy"]
