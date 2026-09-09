from __future__ import annotations

from uuid import uuid4

import pytest

from agentic_osdu.formats import BytesFormatSource, FormatExtractionError
from agentic_osdu.formats.segy import extract_segy
from agentic_osdu.formats.well_logs import extract_dlis
from agentic_osdu.tools.contracts import ExtractDlisInput, ExtractSegyInput


def test_extractors_reject_a_source_capability_for_a_different_file_id() -> None:
    source = BytesFormatSource(file_id=uuid4(), content=b" " * 3600)
    with pytest.raises(FormatExtractionError, match="FILE_CHANGED"):
        extract_segy(ExtractSegyInput(file_id=uuid4()), source)


def test_native_parser_errors_do_not_expose_paths_or_raw_content() -> None:
    secret_path = r"C:\customers\restricted\Volve-F1.dlis"
    raw_secret = "CONFIDENTIAL-WELL-CONTENT"
    item = BytesFormatSource(file_id=uuid4(), content=raw_secret.encode())

    def broken(_: object) -> object:
        raise RuntimeError(f"failed at {secret_path}: {raw_secret}")

    with pytest.raises(FormatExtractionError) as failure:
        extract_dlis(
            ExtractDlisInput(file_id=item.file_id),
            item,
            loader=broken,
            library_version="test",
        )

    message = str(failure.value)
    assert failure.value.code == "DLIS_OPEN_FAILED"
    assert secret_path not in message
    assert raw_secret not in message


def test_bytes_source_is_read_only_and_bounded() -> None:
    content = b"0123456789"
    item = BytesFormatSource(file_id=uuid4(), content=content)
    with item.open_binary(max_bytes=4) as stream:
        assert stream.read() == b"0123"
    assert item.content == content
