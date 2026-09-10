from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class FixtureEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_id: str
    format_id: str
    relative_path: str
    source_kind: str
    recipe: str
    size_bytes: int
    sha256: str

    def verify(self, payload: bytes) -> bool:
        return hashlib.sha256(payload).hexdigest() == self.sha256


class FixtureCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: str
    fixtures: tuple[FixtureEntry, ...]


def load_catalog(path: Path) -> FixtureCatalog:
    return FixtureCatalog.model_validate_json(path.read_text(encoding="utf-8"))


def catalog_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def materialize_catalog(catalog: FixtureCatalog, root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for fixture in catalog.fixtures:
        payload = _payload(fixture.recipe)
        if len(payload) != fixture.size_bytes or not fixture.verify(payload):
            raise ValueError(f"Fixture catalog hash mismatch: {fixture.fixture_id}")
        path = root / fixture.relative_path
        path.write_bytes(payload)
        result[fixture.fixture_id] = path
    return result


def _payload(recipe: str) -> bytes:
    if recipe == "segy_minimal":
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
    values = {
        "las_minimal": (
            b"~Version Information\nVERS. 2.0 : version\n~Well Information\n"
            b"WELL. : Volve F-1\n~Curve Information\nDEPT.M : Depth\n"
            b"GR.API : Gamma\n~ASCII\n1000 10\n"
        ),
        "json_well_log_minimal": json.dumps(
            [
                {
                    "header": {"name": "Main", "well": "Volve F-1"},
                    "curves": [{"name": "DEPT", "unit": "m", "valueType": "float"}],
                    "data": [[1000.0]],
                }
            ],
            separators=(",", ":"),
        ).encode(),
        "dlis_signature": b"V1.00 RECORD\nSYNTHETIC RP66 FIXTURE\n",
        "lis_signature": b"LIS79 RECORD\nSYNTHETIC LOGICAL RECORD\n",
        "csv_minimal": b"MD,GR\n1000,10\n1001,11\n",
        "p190_minimal": (
            ("H1800" + (" " * 27) + "U.T.M")
            + "\n"
            + ("H1400" + (" " * 27) + "WGS 84")
            + "\n"
            + ("H1900" + (" " * 27) + "31 NORTH")
            + "\n"
            + (
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
            + "\n"
        ).encode(),
        "sgp_minimal": b"H 1 1 100.0 200.0\nH 1 2 101.0 200.0\n1 2\n3 4\n",
        "dat_fault_minimal": (
            b"# Source cartographic system name: EPSG:32631\nFAULT\n1,2,100.0,200.0,300.0\n"
        ),
        "text_minimal": b"Volve supporting document\nSecond bounded line\n",
        "pdf_minimal": b"%PDF-1.7\n% synthetic fixture\n",
        "manifest_minimal": json.dumps(
            {
                "kind": "osdu:wks:Manifest:1.0.0",
                "Data": {
                    "WorkProduct": {
                        "id": "surrogate-key:wp-1",
                        "kind": "osdu:wks:work-product--WorkProduct:1.0.0",
                        "data": {"Components": []},
                    },
                    "WorkProductComponents": [],
                    "Datasets": [],
                },
            },
            separators=(",", ":"),
        ).encode(),
        "schema_minimal": json.dumps(
            {
                "$schema": "https://json-schema.org/draft-07/schema#",
                "$id": "osdu:wks:work-product-component--WellLog:1.1.0",
                "type": "object",
                "properties": {"kind": {"type": "string"}},
            },
            separators=(",", ":"),
        ).encode(),
    }
    try:
        return values[recipe]
    except KeyError as error:
        raise ValueError(f"Unknown fixture recipe: {recipe}") from error
