"""TOOL-007..010 deterministic LAS, JSON Well Log, DLIS, and LIS extractors."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from io import TextIOWrapper
from typing import Any

import ijson  # type: ignore[import-untyped]
from pydantic import ValidationError

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
    DlisChannelMetadata,
    DlisFrameMetadata,
    DlisMetadata,
    DlisOriginMetadata,
    ExtractDlisInput,
    ExtractJsonWellLogInput,
    ExtractLasInput,
    ExtractLisInput,
    JsonCurveMetadata,
    JsonLogSetMetadata,
    JsonWellLogMetadata,
    LasCurveMetadata,
    LasMetadata,
    LisMetadata,
)

NativeLoader = Callable[[object], object]
_VALUE_TYPES = {"float", "integer", "string", "datetime", "boolean"}


def _safe_text(value: object, maximum: int = 256) -> str | None:
    if value is None:
        return None
    return str(value).strip()[:maximum] or None


def _las_line(line: str) -> tuple[str, str, str, str] | None:
    import re

    match = re.match(r"\s*([^.#\s]+)\s*\.\s*([^\s:]*)\s*(.*?)\s*:\s*(.*)$", line)
    if match is None:
        return None
    return (
        match.group(1).upper(),
        match.group(2),
        match.group(3).strip(),
        match.group(4).strip(),
    )


def _section_name(line: str) -> str:
    value = line.lstrip()[1:].strip().split(maxsplit=1)[0].upper()
    return {
        "V": "VERSION",
        "VERSION": "VERSION",
        "W": "WELL",
        "WELL": "WELL",
        "C": "CURVE",
        "CURVE": "CURVE",
        "A": "ASCII",
        "ASCII": "ASCII",
        "P": "PARAMETER",
        "PARAMETER": "PARAMETER",
        "O": "OTHER",
        "OTHER": "OTHER",
    }.get(value, value)


def extract_las(
    request: ExtractLasInput,
    source: FormatSource,
    *,
    cancellation: CancellationCheck | None = None,
) -> ExtractionOutcome[LasMetadata]:
    validate_source(request.file_id, source)
    encoding = request.options.encoding or "latin-1"
    sections: list[str] = []
    curves: list[LasCurveMetadata] = []
    values: dict[str, str] = {}
    current = ""
    rows = 0
    try:
        with (
            source.open_binary() as binary,
            TextIOWrapper(binary, encoding=encoding, errors="strict") as text,
        ):
            for line_number, raw_line in enumerate(text, start=1):
                check_cancelled(cancellation)
                if line_number > request.options.max_lines:
                    raise FormatExtractionError(
                        "LAS_SECTION_INVALID", "LAS input exceeds the configured line bound."
                    )
                line = raw_line.rstrip("\r\n")
                if line.lstrip().startswith("~"):
                    current = _section_name(line)
                    sections.append(current)
                    continue
                if current == "ASCII":
                    if line.strip() and not line.lstrip().startswith("#"):
                        rows += 1
                    continue
                parsed = _las_line(line)
                if parsed is None:
                    continue
                mnemonic, unit, value, description = parsed
                if current == "CURVE":
                    curves.append(
                        LasCurveMetadata(
                            mnemonic=mnemonic,
                            unit=unit or None,
                            description=(
                                description or None
                                if request.options.include_curve_descriptions
                                else None
                            ),
                        )
                    )
                elif current in {"VERSION", "WELL"}:
                    values[mnemonic] = value or unit or description
    except FormatExtractionError:
        raise
    except (LookupError, UnicodeDecodeError) as error:
        raise FormatExtractionError(
            "LAS_ENCODING_FAILED", "LAS input cannot be decoded with the selected encoding."
        ) from error
    except ValidationError as error:
        raise FormatExtractionError(
            "LAS_SECTION_INVALID", "A LAS mnemonic field exceeds the supported contract."
        ) from error
    if not sections or "VERSION" not in sections:
        raise FormatExtractionError("LAS_SECTION_INVALID", "A LAS version section is required.")
    return ExtractionOutcome(
        output=LasMetadata(
            version=values.get("VERS") or None,
            well_name=values.get("WELL") or values.get("UWI") or None,
            sections=tuple(sections),
            curves=tuple(curves),
            row_count=rows if "ASCII" in sections else None,
        ),
        evidence=(
            evidence(
                source.file_id,
                "LAS-1.SECTIONS",
                "LAS section markers and mnemonic records were parsed.",
                location=f"lines 1-{line_number}",
                observed_value=",".join(sections),
            ),
        ),
    )


def _plain(value: object) -> object:
    if isinstance(value, Decimal):
        integral = value.to_integral_value()
        return int(integral) if value == integral else float(value)
    return value


def _cell_matches(event: str, value: object, curve: dict[str, object]) -> bool:
    expected = curve.get("valueType") or "float"
    dimensions = curve.get("dimensions") or 1
    cells: list[tuple[str, object]]
    if isinstance(dimensions, int) and dimensions > 1:
        if event != "array" or not isinstance(value, list) or len(value) != dimensions:
            return False
        cells = value
    else:
        if event == "array":
            return False
        cells = [(event, value)]
    for cell_event, cell_value in cells:
        if cell_event == "null":
            continue
        if expected in {"float", "integer"}:
            if cell_event != "number":
                return False
            if expected == "integer" and not isinstance(cell_value, int):
                return False
        elif (expected in {"string", "datetime"} and cell_event != "string") or (
            expected == "boolean" and cell_event != "boolean"
        ):
            return False
    return True


def extract_json_well_log(
    request: ExtractJsonWellLogInput,
    source: FormatSource,
    *,
    cancellation: CancellationCheck | None = None,
) -> ExtractionOutcome[JsonWellLogMetadata]:
    """Stream curve definitions and rows without retaining sample arrays."""

    validate_source(request.file_id, source)
    curves: list[dict[str, object]] = []
    header: dict[str, object] = {}
    current_curve: dict[str, object] | None = None
    current_row: list[tuple[str, object]] | None = None
    current_array: list[tuple[str, object]] | None = None
    log_row_count = 0
    total_row_count = 0
    total_curve_count = 0
    first_index: object = None
    last_index: object = None
    previous_index: float | None = None
    data_seen = False
    data_uri: str | None = None
    log_sets: list[JsonLogSetMetadata] = []
    allowed_keys = {"header", "curves", "data"}
    try:
        with source.open_binary() as stream:
            parser = ijson.parse(stream)
            first = next(parser, None)
            if first is None or first[0] != "" or first[1] != "start_array":
                raise FormatExtractionError(
                    "JSON_INVALID", "The JSON Well Log root must be an array."
                )
            for prefix, event, raw_value in parser:
                check_cancelled(cancellation)
                value = _plain(raw_value)
                if prefix == "item" and event in {
                    "start_array",
                    "string",
                    "number",
                    "boolean",
                    "null",
                }:
                    raise FormatExtractionError(
                        "JSON_INVALID", "Every JSON Well Log array member must be an object."
                    )
                if prefix == "item" and event == "start_map":
                    curves = []
                    header = {}
                    current_curve = None
                    current_row = None
                    current_array = None
                    log_row_count = 0
                    first_index = None
                    last_index = None
                    previous_index = None
                    data_seen = False
                    data_uri = None
                    continue
                if prefix == "item" and event == "map_key":
                    if not isinstance(value, str) or value not in allowed_keys:
                        raise FormatExtractionError(
                            "JSON_INVALID", "A log set contains an unsupported top-level member."
                        )
                    continue
                if prefix.startswith("item.header.") and event in {
                    "string",
                    "number",
                    "boolean",
                    "null",
                }:
                    parts = prefix.split(".")
                    if len(parts) == 3:
                        header[parts[2]] = value
                        if parts[2] == "dataUri":
                            if not isinstance(value, str) or not value.strip():
                                raise FormatExtractionError(
                                    "JSON_INVALID",
                                    "The external data URI must be a non-empty string.",
                                )
                            data_uri = value.strip()
                            if len(data_uri) > 2048:
                                raise FormatExtractionError(
                                    "JSON_INVALID",
                                    "The external data URI exceeds the configured bound.",
                                )
                    continue
                if prefix == "item.curves.item" and event == "start_map":
                    current_curve = {}
                    continue
                if current_curve is not None and prefix.startswith("item.curves.item."):
                    parts = prefix.split(".")
                    if len(parts) == 4 and event in {
                        "string",
                        "number",
                        "boolean",
                        "null",
                    }:
                        current_curve[parts[3]] = value
                    continue
                if prefix == "item.curves.item" and event == "end_map":
                    if current_curve is None:
                        raise FormatExtractionError(
                            "CURVE_SCHEMA_INVALID",
                            "A curve end marker has no matching curve definition.",
                        )
                    name = current_curve.get("name")
                    value_type = current_curve.get("valueType", "float")
                    dimensions = current_curve.get("dimensions", 1)
                    if (
                        not isinstance(name, str)
                        or not name.strip()
                        or value_type not in _VALUE_TYPES
                        or not isinstance(dimensions, int)
                        or isinstance(dimensions, bool)
                        or dimensions < 1
                    ):
                        raise FormatExtractionError(
                            "CURVE_SCHEMA_INVALID", "A curve definition is invalid."
                        )
                    curves.append(current_curve)
                    total_curve_count += 1
                    if total_curve_count > request.options.max_curves:
                        raise FormatExtractionError(
                            "CURVE_SCHEMA_INVALID",
                            "Curve definitions exceed the configured bound.",
                        )
                    current_curve = None
                    continue
                if prefix == "item.data" and event == "start_array":
                    data_seen = True
                    continue
                if prefix == "item.data.item" and event == "start_array":
                    current_row = []
                    continue
                if current_row is not None and prefix == "item.data.item.item":
                    if event == "start_array":
                        current_array = []
                    elif event in {"string", "number", "boolean", "null"}:
                        current_row.append((event, value))
                    elif event == "end_array" and current_array is not None:
                        current_row.append(("array", current_array))
                        current_array = None
                    continue
                if (
                    current_array is not None
                    and prefix == "item.data.item.item.item"
                    and event in {"string", "number", "boolean", "null"}
                ):
                    current_array.append((event, value))
                    continue
                if prefix == "item.data.item" and event == "end_array":
                    if current_row is None or len(current_row) != len(curves):
                        raise FormatExtractionError(
                            "CURVE_SCHEMA_INVALID",
                            "A data row does not match the curve-definition count.",
                        )
                    log_row_count += 1
                    total_row_count += 1
                    if total_row_count > request.options.max_rows:
                        raise FormatExtractionError(
                            "JSON_LIMIT_EXCEEDED", "Data rows exceed the configured bound."
                        )
                    if request.options.validate_cell_types:
                        for cell, curve in zip(current_row, curves, strict=True):
                            if not _cell_matches(cell[0], cell[1], curve):
                                raise FormatExtractionError(
                                    "CELL_TYPE_MISMATCH",
                                    "A streamed cell does not match its declared curve type.",
                                )
                    index_event, index_value = current_row[0]
                    if index_event == "null" or index_event == "array":
                        raise FormatExtractionError(
                            "INDEX_INCONSISTENT", "The index curve contains an invalid value."
                        )
                    if first_index is None:
                        first_index = index_value
                    last_index = index_value
                    if isinstance(index_value, int | float):
                        numeric = float(index_value)
                        declared_step = header.get("step")
                        if (
                            request.options.validate_index
                            and previous_index is not None
                            and isinstance(declared_step, int | float)
                            and abs((numeric - previous_index) - float(declared_step)) > 0.001
                        ):
                            raise FormatExtractionError(
                                "INDEX_INCONSISTENT",
                                "Streamed index increments differ from the declared step.",
                            )
                        previous_index = numeric
                    current_row = None
                    continue
                if prefix == "item" and event == "end_map":
                    name = header.get("name")
                    if not isinstance(name, str) or not name.strip() or not curves:
                        raise FormatExtractionError(
                            "JSON_INVALID",
                            "A complete named log set with curve definitions is required.",
                        )
                    if (not data_seen or log_row_count == 0) and data_uri is None:
                        raise FormatExtractionError(
                            "JSON_INVALID",
                            "A log set requires non-empty inline data or a valid data URI.",
                        )
                    if request.options.validate_index:
                        declared_start = header.get("startIndex")
                        declared_end = header.get("endIndex")
                        if (
                            declared_start is not None
                            and first_index is not None
                            and declared_start != first_index
                        ) or (
                            declared_end is not None
                            and last_index is not None
                            and declared_end != last_index
                        ):
                            raise FormatExtractionError(
                                "INDEX_INCONSISTENT",
                                "Declared index bounds differ from streamed samples.",
                            )
                    typed_curves = tuple(
                        JsonCurveMetadata(
                            name=str(curve["name"]),
                            unit=_safe_text(curve.get("unit"), 64),
                            value_type=_safe_text(curve.get("valueType") or "float", 64),
                        )
                        for curve in curves
                    )
                    log_sets.append(
                        JsonLogSetMetadata(
                            name=name.strip(),
                            well_name=_safe_text(header.get("well") or header.get("wellbore")),
                            curves=typed_curves,
                            row_count=log_row_count,
                            column_count=len(typed_curves),
                            index_curve=typed_curves[0].name,
                            data_uri=data_uri,
                        )
                    )
    except FormatExtractionError:
        raise
    except (ijson.JSONError, UnicodeError, ValueError, RecursionError) as error:
        raise FormatExtractionError(
            "JSON_INVALID", "JSON Well Log syntax or structure is invalid."
        ) from error
    if not log_sets:
        raise FormatExtractionError("JSON_INVALID", "At least one complete log set is required.")
    primary = log_sets[0]
    return ExtractionOutcome(
        output=JsonWellLogMetadata(
            well_name=primary.well_name,
            curves=primary.curves,
            row_count=primary.row_count,
            column_count=primary.column_count,
            index_curve=primary.index_curve,
            log_set_count=len(log_sets),
            log_sets=tuple(log_sets),
        ),
        evidence=(
            evidence(
                source.file_id,
                "JSON-WELL-1.STREAM",
                "Curve definitions and data rows were validated as parser events.",
                location="/",
                observed_value=f"{len(log_sets)} logs/{total_row_count} rows",
            ),
        ),
    )


@contextmanager
def _native_result(loaded: object) -> Iterator[Iterable[Any]]:
    enter = getattr(loaded, "__enter__", None)
    exit_method = getattr(loaded, "__exit__", None)
    if callable(enter) and callable(exit_method):
        value = enter()
        try:
            yield value
        finally:
            exit_method(None, None, None)
    else:
        yield loaded  # type: ignore[misc]


def _default_dlis_loader(locator: object) -> object:
    from dlisio import dlis  # type: ignore[import-untyped]

    return dlis.load(locator)


def _default_lis_loader(locator: object) -> object:
    from dlisio import lis

    return lis.load(locator)


def extract_dlis(
    request: ExtractDlisInput,
    source: FormatSource,
    *,
    loader: NativeLoader = _default_dlis_loader,
    library_version: str | None = None,
    cancellation: CancellationCheck | None = None,
) -> ExtractionOutcome[DlisMetadata]:
    validate_source(request.file_id, source)
    logical_ids: list[str] = []
    frames: list[DlisFrameMetadata] = []
    channels: list[DlisChannelMetadata] = []
    origins: list[DlisOriginMetadata] = []
    wells: list[str] = []
    try:
        with source.open_native() as locator:
            try:
                loaded = loader(locator)
            except (OSError, RuntimeError, ValueError, TypeError, UnicodeError) as error:
                raise FormatExtractionError(
                    "DLIS_OPEN_FAILED", "The DLIS library could not open the approved source."
                ) from error
            with _native_result(loaded) as logical_files:
                for logical_number, logical in enumerate(logical_files, start=1):
                    check_cancelled(cancellation)
                    if logical_number > request.options.max_logical_files:
                        raise FormatExtractionError(
                            "DLIS_LOGICAL_FILE_INVALID",
                            "Logical files exceed the configured bound.",
                        )
                    logical_id = (
                        _safe_text(getattr(getattr(logical, "fileheader", None), "id", None))
                        or f"logical-{logical_number}"
                    )
                    logical_ids.append(logical_id)
                    logical_channels = list(getattr(logical, "channels", ()))
                    for frame in getattr(logical, "frames", ()):
                        frame_channels = [
                            channel
                            for channel in getattr(frame, "channels", ())
                            if channel is not None
                        ]
                        frames.append(
                            DlisFrameMetadata(
                                logical_file_id=logical_id,
                                frame_id=_safe_text(getattr(frame, "name", None))
                                or f"frame-{len(frames) + 1}",
                                channel_count=len(frame_channels),
                            )
                        )
                    for channel in logical_channels:
                        if len(channels) >= request.options.max_channels:
                            raise FormatExtractionError(
                                "DLIS_LOGICAL_FILE_INVALID",
                                "Channels exceed the configured bound.",
                            )
                        name = _safe_text(getattr(channel, "name", None), 128)
                        channels.append(
                            DlisChannelMetadata(
                                logical_file_id=logical_id,
                                channel_id=name or f"channel-{len(channels) + 1}",
                                mnemonic=name,
                                unit=_safe_text(getattr(channel, "units", None), 64),
                            )
                        )
                    for origin_number, origin in enumerate(
                        getattr(logical, "origins", ()), start=1
                    ):
                        well = _safe_text(getattr(origin, "well_name", None))
                        if well:
                            wells.append(well)
                        origins.append(
                            DlisOriginMetadata(
                                logical_file_id=logical_id,
                                origin_id=_safe_text(getattr(origin, "id", None))
                                or f"origin-{origin_number}",
                                well_name=well,
                            )
                        )
    except FormatExtractionError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, UnicodeError) as error:
        raise FormatExtractionError(
            "DLIS_LIBRARY_ERROR", "The DLIS library could not index the approved source."
        ) from error
    _ = source.size_bytes
    if not logical_ids:
        raise FormatExtractionError(
            "DLIS_LOGICAL_FILE_INVALID", "The DLIS source contains no logical files."
        )
    if library_version is None:
        try:
            import dlisio

            library_version = dlisio.__version__
        except (ImportError, AttributeError):
            library_version = "unknown"
    return ExtractionOutcome(
        output=DlisMetadata(
            logical_file_ids=tuple(logical_ids),
            frames=tuple(frames),
            channels=tuple(channels),
            origins=tuple(origins),
            well_name=next(iter(dict.fromkeys(wells)), None),
            library_version=library_version,
        ),
        evidence=(
            evidence(
                source.file_id,
                "DLIS-1.LOGICAL-FILES",
                "The pinned DLIS library indexed logical objects.",
                location="RP66 logical files",
                observed_value=str(len(logical_ids)),
            ),
        ),
    )


def extract_lis(
    request: ExtractLisInput,
    source: FormatSource,
    *,
    loader: NativeLoader = _default_lis_loader,
    cancellation: CancellationCheck | None = None,
) -> ExtractionOutcome[LisMetadata]:
    validate_source(request.file_id, source)
    record_types: list[str] = []
    record_count = 0
    metadata_count = 0
    well_name: str | None = None
    try:
        with (
            source.open_native() as locator,
            _native_result(loader(locator)) as logical_files,
        ):
            for logical in logical_files:
                check_cancelled(cancellation)
                logical_well: str | None = None
                for accessor_name in ("wellsite_data", "job_identification"):
                    accessor = getattr(logical, accessor_name, None)
                    if not callable(accessor):
                        continue
                    for record in accessor():
                        components = getattr(record, "components", None)
                        entries = (
                            components()
                            if callable(components)
                            else getattr(record, "entries", record)
                        )
                        for entry in entries:
                            check_cancelled(cancellation)
                            metadata_count += 1
                            if metadata_count > request.options.max_records:
                                raise FormatExtractionError(
                                    "LIS_TRUNCATED",
                                    "LIS metadata records exceed the configured bound.",
                                )
                            mnemonic = (
                                _safe_text(getattr(entry, "mnemonic", None), 64) or ""
                            ).upper()
                            value = _safe_text(
                                getattr(entry, "value", None)
                                or getattr(entry, "data", None)
                                or getattr(entry, "component", None)
                                or getattr(entry, "description", None)
                            )
                            if mnemonic in {"WELL", "WN", "WELL-NAME", "WELL NAME", "UWI"}:
                                logical_well = logical_well or value
                well_name = well_name or logical_well
                for specification in logical.data_format_specs():
                    check_cancelled(cancellation)
                    record_count += 1
                    if record_count > request.options.max_records:
                        raise FormatExtractionError(
                            "LIS_TRUNCATED", "LIS records exceed the configured bound."
                        )
                    record_types.extend(
                        _safe_text(getattr(channel, "mnemonic", None), 128) or "unknown"
                        for channel in getattr(specification, "specs", ())
                    )
    except FormatExtractionError:
        raise
    except UnicodeError as error:
        raise FormatExtractionError(
            "LIS_ENCODING_FAILED", "LIS text metadata could not be decoded."
        ) from error
    except (OSError, RuntimeError, ValueError, TypeError, NotImplementedError) as error:
        raise FormatExtractionError(
            "LIS_RECORD_INVALID", "The LIS library could not index the approved source."
        ) from error
    if record_count == 0:
        raise FormatExtractionError(
            "LIS_RECORD_INVALID", "The LIS source contains no data-format records."
        )
    _ = source.size_bytes
    return ExtractionOutcome(
        output=LisMetadata(
            record_count=record_count,
            record_types=tuple(dict.fromkeys(record_types)),
            well_name=well_name,
            truncated=False,
        ),
        evidence=(
            evidence(
                source.file_id,
                "LIS-1.RECORDS",
                "LIS data-format records and channel mnemonics were indexed.",
                location="LIS logical records",
                observed_value=str(record_count),
            ),
        ),
    )


__all__ = [
    "extract_dlis",
    "extract_json_well_log",
    "extract_las",
    "extract_lis",
]
