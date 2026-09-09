"""TOOL-012 deterministic bounded UKOOA P1/90 extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import TextIOWrapper

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
    ExtractP190Input,
    NavigationPosition,
    P190HeaderField,
    P190LineSummary,
    P190Metadata,
)

_HEADER_FIELDS = {
    "H0100": "surveyArea",
    "H0202": "formatVersion",
    "H1400": "surveyedDatum",
    "H1500": "postDatum",
    "H1800": "projection",
    "H1900": "projectionZone",
    "H2000": "gridUnits",
}


@dataclass(slots=True)
class _LineStats:
    count: int
    first_point: int
    last_point: int
    point_increment: int | None
    easting_min: float
    easting_max: float
    northing_min: float
    northing_max: float
    water_depth_min: float | None
    water_depth_max: float | None


def _dms(value: str, degree_digits: int) -> float | None:
    match = re.fullmatch(
        rf"\s*(\d{{{degree_digits}}})(\d{{2}})(\d{{2}}(?:\.\d+)?)\s*([NSEW])",
        value,
    )
    if match is None:
        return None
    degrees, minutes, seconds = map(float, match.group(1, 2, 3))
    maximum = 90 if match.group(4) in {"N", "S"} else 180
    if (
        minutes >= 60
        or seconds >= 60
        or degrees > maximum
        or (degrees == maximum and (minutes != 0 or seconds != 0))
    ):
        return None
    result = degrees + minutes / 60 + seconds / 3600
    return -result if match.group(4) in {"S", "W"} else result


def _epsg(headers: dict[str, str]) -> int | None:
    datum = (headers.get("postDatum") or headers.get("surveyedDatum") or "").upper()
    projection = headers.get("projection", "").upper()
    zone_text = headers.get("projectionZone", "")
    if "NZGD2000" in datum and "NZTM" in projection:
        return 2193
    match = re.search(r"\b(\d{1,2})\b", zone_text)
    if match is None or ("UTM" not in projection and "U.T.M" not in projection):
        return None
    zone = int(match.group(1))
    northern = "SOUTH" not in zone_text.upper()
    if "WGS" in datum and "84" in datum:
        return (32600 if northern else 32700) + zone
    if "ED50" in datum and northern:
        return 23000 + zone
    if "ETRS89" in datum and northern:
        return 25800 + zone
    return None


def extract_p190(
    request: ExtractP190Input,
    source: FormatSource,
    *,
    cancellation: CancellationCheck | None = None,
) -> ExtractionOutcome[P190Metadata]:
    validate_source(request.file_id, source)
    headers: dict[str, str] = {}
    header_items: list[P190HeaderField] = []
    positions: list[NavigationPosition] = []
    line_ranges: dict[str, _LineStats] = {}
    try:
        with (
            source.open_binary() as binary,
            TextIOWrapper(binary, encoding="ascii", errors="strict") as text,
        ):
            for line_number, raw_line in enumerate(text, start=1):
                check_cancelled(cancellation)
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                if line.startswith("H"):
                    code = line[:5]
                    value = (
                        line.split(":", 1)[1].strip() if ":" in line[5:32] else line[32:].strip()
                    )
                    header_items.append(
                        P190HeaderField(code=code, value=value[:512], line_number=line_number)
                    )
                    field = _HEADER_FIELDS.get(code)
                    if field is not None:
                        headers[field] = value
                    if code == "H2600" and "NZGD2000" in value.upper():
                        headers["surveyedDatum"] = "NZGD2000"
                    continue
                if line[0] not in {"S", "R", "C", "Q", "T", "V"} or len(line) < 64:
                    continue
                try:
                    point = int(line[19:25])
                    latitude = _dms(line[25:35], 2)
                    longitude = _dms(line[35:46], 3)
                    easting = float(line[46:55])
                    northing = float(line[55:64])
                    depth_text = line[64:70].strip()
                    water_depth = float(depth_text) if depth_text else None
                except ValueError:
                    continue
                line_name = line[1:13].strip()
                if not line_name or latitude is None or longitude is None:
                    continue
                try:
                    position = NavigationPosition(
                        line_name=line_name,
                        point_number=point,
                        latitude=latitude,
                        longitude=longitude,
                        easting=easting,
                        northing=northing,
                        water_depth=water_depth,
                    )
                except ValidationError:
                    continue
                current = line_ranges.get(line_name)
                if current is None:
                    line_ranges[line_name] = _LineStats(
                        count=1,
                        first_point=point,
                        last_point=point,
                        point_increment=None,
                        easting_min=easting,
                        easting_max=easting,
                        northing_min=northing,
                        northing_max=northing,
                        water_depth_min=water_depth,
                        water_depth_max=water_depth,
                    )
                else:
                    if current.point_increment is None:
                        current.point_increment = point - current.last_point
                    current.count += 1
                    current.last_point = point
                    current.easting_min = min(current.easting_min, easting)
                    current.easting_max = max(current.easting_max, easting)
                    current.northing_min = min(current.northing_min, northing)
                    current.northing_max = max(current.northing_max, northing)
                    if water_depth is not None:
                        current.water_depth_min = (
                            water_depth
                            if current.water_depth_min is None
                            else min(current.water_depth_min, water_depth)
                        )
                        current.water_depth_max = (
                            water_depth
                            if current.water_depth_max is None
                            else max(current.water_depth_max, water_depth)
                        )
                if len(positions) < request.options.max_positions:
                    positions.append(position)
    except UnicodeDecodeError as error:
        raise FormatExtractionError(
            "P190_HEADER_INVALID", "P1/90 records are not valid ASCII."
        ) from error
    if not line_ranges:
        raise FormatExtractionError(
            "P190_POSITION_INVALID", "No valid fixed-width P1/90 position record was found."
        )
    summaries = tuple(
        P190LineSummary(
            line_name=name,
            position_count=values.count,
            first_point_number=values.first_point,
            last_point_number=values.last_point,
            point_increment=values.point_increment,
            easting_min=values.easting_min,
            easting_max=values.easting_max,
            northing_min=values.northing_min,
            northing_max=values.northing_max,
            water_depth_min=values.water_depth_min,
            water_depth_max=values.water_depth_max,
        )
        for name, values in line_ranges.items()
    )
    inferred = _epsg(headers) if request.options.infer_epsg else None
    return ExtractionOutcome(
        output=P190Metadata(
            headers=tuple(header_items),
            line_names=tuple(line_ranges),
            line_summaries=summaries,
            position_count=sum(values.count for values in line_ranges.values()),
            sampled_positions=tuple(positions),
            inferred_epsg=inferred,
        ),
        evidence=(
            evidence(
                source.file_id,
                "P190-1.POSITIONS",
                "Fixed-width DMS position records were parsed and converted.",
                location="position records",
                observed_value=str(sum(values.count for values in line_ranges.values())),
            ),
            evidence(
                source.file_id,
                "P190-1.EPSG",
                "Datum, projection, hemisphere, and zone rules were evaluated.",
                location="header records",
                observed_value=str(inferred) if inferred else "ambiguous",
            ),
        ),
    )


__all__ = ["extract_p190"]
