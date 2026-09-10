from __future__ import annotations

import contextlib
import importlib
import json
import re
import subprocess
import sys
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def main() -> None:
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    volve_root = Path(request["volve_root"])
    fixture_root = Path(request["fixture_root"])
    sys.path.insert(0, str(volve_root))
    classify_file = importlib.import_module("classifier").classify_file
    manifest_index = importlib.import_module("manifest_index")
    load_manifests = manifest_index.load_manifests
    score_manifest = manifest_index.score_manifest
    manifest_validation = importlib.import_module("manifest_validation")
    schema_relative_path = manifest_validation.schema_relative_path
    manifest_learning = _load_current_learning_module()

    formats: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    for fixture in request["fixtures"]:
        format_id = fixture["format_id"]
        path = fixture_root / fixture["relative_path"]
        status = "supported"
        detected: str | None = None
        record: dict[str, Any] | None = None
        try:
            if format_id == "FMT-012":
                loaded = load_manifests(fixture_root)
                if not any(item["path"] == fixture["relative_path"] for item in loaded):
                    raise ValueError("Manifest fixture was not loaded.")
                detected = format_id
            elif format_id == "FMT-013":
                mapped = schema_relative_path("osdu:wks:work-product-component--WellLog:1.1.0")
                if mapped.as_posix() != "work-product-component/WellLog.1.1.0.json":
                    raise ValueError("Schema mapping changed.")
                detected = format_id
            elif format_id in {"FMT-004", "FMT-005"}:
                detected = format_id if _signature_valid(format_id, path.read_bytes()) else None
                status = "signature-only" if detected else "structured-error:INVALID_SIGNATURE"
            else:
                record = classify_file(path, fixture_root)
                if not isinstance(record.get("details"), dict):
                    raise ValueError("Classifier did not return bounded details.")
                detected = _current_format_id(record["format"])
                if detected != format_id:
                    status = "classification-mismatch"
        except Exception as error:
            status = f"structured-error:{type(error).__name__}"
        formats.append(
            {
                "fixture_id": fixture["fixture_id"],
                "format_id": format_id,
                "detected_format_id": detected,
                "status": status,
            }
        )
        metadata[format_id] = _current_metadata(format_id, path, record)
    payload = {
        "formats": formats,
        "metadata": metadata,
        "associations": _association_probe(score_manifest),
        "learning": {
            "generated_examples_rejected": _current_rejects_generated_examples(
                manifest_learning,
                fixture_root,
            ),
        },
        "generation": _generation_probe(manifest_learning, fixture_root),
        "validation": {
            "kind_mapping": schema_relative_path(
                "osdu:wks:work-product-component--WellLog:1.1.0"
            ).as_posix(),
            "implicit_network_disabled": _current_implicit_network_disabled(
                manifest_validation,
                fixture_root / "schema-probe",
            ),
        },
        "api_shapes": _current_api_shapes(importlib.import_module("server")),
        "capabilities": _ui_capabilities("current", volve_root / "web"),
    }
    Path(request["response_path"]).write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )


def _current_format_id(label: str) -> str | None:
    normalized = label.casefold()
    mapping = {
        "seg-y": "FMT-001",
        "las": "FMT-002",
        "json well log": "FMT-003",
        "dlis": "FMT-004",
        "lis": "FMT-005",
        "csv": "FMT-006",
        "p1/90": "FMT-007",
        "sgp": "FMT-008",
        "dat": "FMT-009",
        "text": "FMT-010",
        "ascii": "FMT-010",
        "pdf": "FMT-011",
    }
    return next((value for token, value in mapping.items() if token in normalized), None)


def _signature_valid(format_id: str, payload: bytes) -> bool:
    signatures = {
        "FMT-004": rb"^V1\.\d{2} RECORD(?:\r?\n)",
        "FMT-005": rb"^LIS79 RECORD(?:\r?\n)",
    }
    return re.match(signatures[format_id], payload[:128]) is not None


def _current_metadata(
    format_id: str,
    path: Path,
    record: dict[str, Any] | None,
) -> dict[str, Any]:
    if format_id in {"FMT-004", "FMT-005"}:
        return {
            "observation_depth": "unsupported-signature-only",
            "signature_valid": _signature_valid(format_id, path.read_bytes()),
        }
    if format_id == "FMT-012":
        documents = importlib.import_module("manifest_index").load_manifests(path.parent)
        item = next(value for value in documents if value["path"] == path.name)
        return {
            "observation_depth": "extracted",
            "document_kind": item["document"]["kind"],
        }
    if format_id == "FMT-013":
        mapped = importlib.import_module("manifest_validation").schema_relative_path(
            "osdu:wks:work-product-component--WellLog:1.1.0"
        )
        return {"observation_depth": "extracted", "schema_path": mapped.as_posix()}
    if record is None:
        return {"observation_depth": "structured-error"}
    details = record["details"]
    if "parseError" in details:
        return {"observation_depth": "structured-error"}
    if format_id == "FMT-001":
        return {
            "observation_depth": "extracted",
            "sample_interval": details["sampleIntervalUs"],
            "samples_per_trace": details["samplesPerTrace"],
            "sample_format_code": details["sampleFormatCode"],
            "record_count": details["traceCount"],
        }
    if format_id == "FMT-002":
        return {
            "observation_depth": "extracted",
            "curve_names": details["curves"],
        }
    if format_id == "FMT-003":
        return {
            "observation_depth": "extracted",
            "curve_names": [item["mnemonic"] for item in details["curves"]],
            "record_count": details["rowCount"],
            "well_name": details["wellName"],
        }
    if format_id == "FMT-006":
        return {
            "observation_depth": "extracted",
            "column_names": details["columns"],
            "column_count": details["columnCount"],
            "record_count": details["dataRows"],
        }
    if format_id == "FMT-007":
        return {
            "observation_depth": "extracted",
            "line_names": details["lineNames"],
            "record_count": details["positionRecordCount"],
            "epsg": details["sourceEpsg"],
        }
    if format_id == "FMT-008":
        return {
            "observation_depth": "extracted",
            "record_count": details["cornerCount"],
            "column_count": len(details["headerColumns"]) // 2,
        }
    if format_id == "FMT-010":
        return {"observation_depth": "extracted", "record_count": details["lineCount"]}
    if format_id == "FMT-011":
        return {
            "observation_depth": "extracted",
            "signature_valid": path.read_bytes().startswith(b"%PDF-"),
            "size_bytes": path.stat().st_size,
        }
    return {"observation_depth": "classification-only"}


def _association_probe(score_manifest: Any) -> dict[str, Any]:
    path = "data/example-12345678.sgy"
    base = {
        "filename": "unrelated.json",
        "path": "other/unrelated.json",
        "datasetStrings": [],
        "allStrings": [],
    }
    cases = [
        {**base, "datasetStrings": [path], "allStrings": [path]},
        {**base, "allStrings": [path]},
        {**base, "datasetStrings": ["example-12345678.sgy"]},
        {**base, "allStrings": ["example-12345678.sgy"]},
        {**base, "filename": "example_12345678_manifest.json", "path": "data/example.json"},
    ]
    names = {
        "Exact path in Data.Datasets": "exact_dataset_path",
        "Exact file path in manifest": "exact_path",
        "Exact filename in Data.Datasets": "exact_dataset_filename",
        "Exact filename in manifest": "exact_filename",
        "Normalized identifier in manifest filename": "normalized_identifier",
    }
    results = [score_manifest(path, item) for item in cases]
    if any(result is None for result in results):
        raise RuntimeError("Current association precedence probe did not produce all five matches")
    return {
        "methods": [names[result[1]] for result in results if result],
    }


def _load_current_learning_module() -> Any:
    try:
        return importlib.import_module("manifest_learning")
    except ModuleNotFoundError as error:
        if error.name != "pyproj":
            raise
    pyproj = types.ModuleType("pyproj")
    transformer = types.ModuleType("pyproj.transformer")
    pyproj.Transformer = object  # type: ignore[attr-defined]
    transformer.AreaOfInterest = object  # type: ignore[attr-defined]
    transformer.TransformerGroup = object  # type: ignore[attr-defined]
    sys.modules["pyproj"] = pyproj
    sys.modules["pyproj.transformer"] = transformer
    return importlib.import_module("manifest_learning")


def _current_rejects_generated_examples(module: Any, manifest_root: Path) -> bool:
    result = module.learn_manifest_patterns(
        {
            "files": [
                {
                    "path": "well/a.las",
                    "category": "Well log",
                    "osduKind": "work-product-component--WellLog",
                    "manifests": [{"path": "generated/well/a.json"}],
                }
            ]
        },
        manifest_root,
    )
    return bool(result["categories"] == {})


def _generation_probe(module: Any, fixture_root: Path) -> dict[str, Any]:
    base = {
        "category": "Well log",
        "osduKind": "work-product-component--WellLog",
        "format": "LAS",
        "extension": ".las",
        "subtype": "Petrophysical/composite log",
        "details": {},
    }
    source = {
        **base,
        "path": "well/source-unique.las",
        "filename": "source-unique.las",
        "manifests": [{"path": "fmt-012-manifest.json"}],
    }
    record = {
        **base,
        "path": "well/target-unique.las",
        "filename": "target-unique.las",
        "manifests": [],
    }
    learning = module.learn_manifest_patterns({"files": [source]}, fixture_root)
    first = module.generated_manifest_relative_path(record).as_posix()
    second = module.generated_manifest_relative_path(record).as_posix()
    original_validation = module.validate_generated_manifest
    module.validate_generated_manifest = lambda _document: {}
    try:
        module.write_generated_manifest(learning, record, fixture_root / "generated-probe")
        try:
            module.write_generated_manifest(learning, record, fixture_root / "generated-probe")
        except FileExistsError:
            no_overwrite = True
        else:
            no_overwrite = False
    finally:
        module.validate_generated_manifest = original_validation
    return {
        "deterministic_path": first == second,
        "no_overwrite": no_overwrite,
    }


def _current_implicit_network_disabled(module: Any, cache_root: Path) -> bool:
    attempted = False

    def deny_network(*_args: Any, **_kwargs: Any) -> None:
        nonlocal attempted
        attempted = True
        raise OSError("network denied by characterization harness")

    original = module.urlopen
    module.urlopen = deny_network
    try:
        with contextlib.suppress(module.SchemaUnavailableError):
            module.SchemaCatalog(cache_root=cache_root, local_root=None).read_wrapper(
                Path("missing.json")
            )
    finally:
        module.urlopen = original
    return not attempted


def _current_api_shapes(module: Any) -> dict[str, Any]:
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.InventoryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def observed(
        path: str,
        method: str = "GET",
        *,
        route_specific_404: str | None = None,
    ) -> bool:
        request = urllib.request.Request(base + path, data=b"{}" if method == "POST" else None)
        request.method = method
        request.add_header("Content-Type", "application/json")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return bool(response.status != 404)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    return True
                return route_specific_404 is not None and route_specific_404 in error.read().decode(
                    "utf-8", errors="replace"
                )
            except (ConnectionError, OSError):
                if attempt == 2:
                    raise
        return False

    try:
        return {
            "inventory": observed("/api/files") and observed("/api/classify", "POST"),
            "jobs": observed("/api/jobs"),
            "manifests": observed(
                "/api/manifest?path=missing&source=missing",
                route_specific_404="Manifest not found",
            ),
            "learning": observed("/api/learn", "POST") and observed("/api/clear-learning", "POST"),
            "generation": observed("/api/generate-manifest", "POST")
            and observed("/api/generate-all", "POST"),
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _ui_capabilities(application: str, web_root: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["node", str(Path(__file__).with_name("ui_probe.cjs")), application, str(web_root)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"UI characterization failed: {completed.stderr[-2000:]}")
    result: dict[str, Any] = json.loads(completed.stdout)
    return result


if __name__ == "__main__":
    main()
