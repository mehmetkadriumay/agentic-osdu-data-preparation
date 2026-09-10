from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from tests.parity.current_harness import characterize_current_application
from tests.parity.fixtures import catalog_sha256, load_catalog, materialize_catalog
from tests.parity.report import (
    DifferenceClassification,
    ParityReport,
    build_parity_report,
    characterize_target_application,
    sign_off_parity_report,
)

ROOT = Path(__file__).parents[2]
CATALOG = ROOT / "fixtures" / "catalog.json"
VOLVE_ROOT = ROOT.parent / "Volve"


def test_item_049_catalog_covers_every_format_with_verified_small_synthetic_fixtures(
    tmp_path: Path,
) -> None:
    catalog = load_catalog(CATALOG)
    assert [item.format_id for item in catalog.fixtures] == [
        f"FMT-{number:03d}" for number in range(1, 14)
    ]
    paths = materialize_catalog(catalog, tmp_path)
    assert set(paths) == {item.fixture_id for item in catalog.fixtures}
    for item in catalog.fixtures:
        payload = paths[item.fixture_id].read_bytes()
        assert len(payload) == item.size_bytes
        assert item.verify(payload)
        assert item.source_kind == "synthetic"
        assert item.size_bytes <= 16 * 1024


def test_item_050_current_characterization_is_reproducible_and_does_not_mutate_volve(
    tmp_path: Path,
) -> None:
    first = characterize_current_application(CATALOG, VOLVE_ROOT, tmp_path / "first")
    second = characterize_current_application(CATALOG, VOLVE_ROOT, tmp_path / "second")

    assert first == second
    assert first.source_unchanged is True
    assert first.fixture_catalog_sha256 == second.fixture_catalog_sha256
    assert {item["format_id"] for item in first.formats} == {
        f"FMT-{number:03d}" for number in range(1, 14)
    }


def test_item_051_comparison_classifies_every_difference_and_fails_closed(
    tmp_path: Path,
) -> None:
    current = characterize_current_application(CATALOG, VOLVE_ROOT, tmp_path / "current")
    target = characterize_target_application(CATALOG, tmp_path / "target")
    report = build_parity_report(current, target)

    assert report.summary.total > 0
    assert report.summary.total == len(report.comparisons)
    assert all(
        item.classification
        in {
            DifferenceClassification.EQUAL,
            DifferenceClassification.INTENTIONAL_APPROVED,
        }
        for item in report.comparisons
    )
    assert report.summary.blocking == 0
    assert report.acceptance.ac_014_automated_passed is True
    assert report.acceptance.human_sign_off == "pending"

    changed = target.model_copy(
        update={
            "capabilities": {
                **target.capabilities,
                "ui.filtering": "missing",
            }
        }
    )
    blocked = build_parity_report(current, changed)
    assert blocked.summary.blocking == 1
    assert blocked.acceptance.ac_014_automated_passed is False
    assert (
        next(
            item for item in blocked.comparisons if item.key == "capabilities.ui.filtering"
        ).classification
        is DifferenceClassification.BLOCKING
    )

    approved_keys = (
        "ui.cancellation",
        "ui.provenance_and_trust",
    )
    for key in approved_keys:
        fabricated = target.model_copy(
            update={"capabilities": {**target.capabilities, key: "fabricated"}}
        )
        result = build_parity_report(current, fabricated)
        comparison = next(item for item in result.comparisons if item.key == f"capabilities.{key}")
        assert comparison.classification is DifferenceClassification.BLOCKING
    fabricated_validation = target.model_copy(
        update={"validation": {**target.validation, "implicit_network_disabled": "fabricated"}}
    )
    comparison = next(
        item
        for item in build_parity_report(current, fabricated_validation).comparisons
        if item.key == "validation.implicit_network_disabled"
    )
    assert comparison.classification is DifferenceClassification.BLOCKING

    fabricated_metadata = target.model_copy(
        update={
            "metadata": {
                **target.metadata,
                "FMT-002": {
                    **target.metadata["FMT-002"],
                    "curve_names": ["fabricated"],
                },
            }
        }
    )
    comparison = next(
        item
        for item in build_parity_report(current, fabricated_metadata).comparisons
        if item.key == "metadata.FMT-002.curve_names"
    )
    assert comparison.classification is DifferenceClassification.BLOCKING

    fabricated_signature = target.model_copy(
        update={
            "metadata": {
                **target.metadata,
                "FMT-004": {
                    **target.metadata["FMT-004"],
                    "signature_valid": "fabricated",
                },
            }
        }
    )
    comparison = next(
        item
        for item in build_parity_report(current, fabricated_signature).comparisons
        if item.key == "metadata.FMT-004.signature_valid"
    )
    assert comparison.classification is DifferenceClassification.BLOCKING


def test_signature_only_fixtures_report_no_fabricated_extraction_depth(
    tmp_path: Path,
) -> None:
    current = characterize_current_application(CATALOG, VOLVE_ROOT, tmp_path / "current")
    target = characterize_target_application(CATALOG, tmp_path / "target")

    for characterization in (current, target):
        for format_id in ("FMT-004", "FMT-005"):
            assert characterization.metadata[format_id] == {
                "observation_depth": "unsupported-signature-only",
                "signature_valid": True,
            }
            result = next(
                item for item in characterization.formats if item["format_id"] == format_id
            )
            assert result["status"] == "signature-only"


def test_item_052_generated_report_is_current_and_has_no_unexplained_capability_loss(
    tmp_path: Path,
) -> None:
    report_path = ROOT / "docs" / "parity-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    current = characterize_current_application(CATALOG, VOLVE_ROOT, tmp_path / "current")
    target = characterize_target_application(CATALOG, tmp_path / "target")
    reproduced = build_parity_report(current, target).model_dump(mode="json")
    signed = sign_off_parity_report(
        build_parity_report(current, target),
        comment="authorized, all good",
        recorded_at=datetime.fromisoformat("2026-09-10T13:35:13.078-07:00"),
        source_commit="51702e705968dc63b3b2bc160ed66ee182bf9bfc",
    )
    sign_off = signed.acceptance.human_sign_off
    assert not isinstance(sign_off, str)

    assert ParityReport.model_validate(report) == signed
    assert report["comparisons"] == reproduced["comparisons"]
    assert report["fixture_results"] == reproduced["fixture_results"]
    assert report["summary"] == reproduced["summary"]
    assert report["fixture_catalog_sha256"] == catalog_sha256(CATALOG)
    assert report["summary"]["blocking"] == 0
    assert report["acceptance"]["ac_014_automated_passed"] is True
    assert report["acceptance"]["human_sign_off"] == {
        "approved": True,
        "comment": "authorized, all good",
        "recorded_at": "2026-09-10T13:35:13.078-07:00",
        "report_sha256": sign_off.report_sha256,
        "source_commit": "51702e705968dc63b3b2bc160ed66ee182bf9bfc",
    }
    assert report["acceptance"]["ready_for_human_sign_off"] is False
    assert {item["format_id"] for item in report["fixture_results"]} == {
        f"FMT-{number:03d}" for number in range(1, 14)
    }
    assert all(
        item["classification"] in {"equal", "intentional-approved", "blocking"}
        for item in report["comparisons"]
    )
