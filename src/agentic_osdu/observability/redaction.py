"""Bounded recursive redaction that never falls back to arbitrary repr or str."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import PurePath
from typing import Any
from uuid import UUID

from pydantic import BaseModel

_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|auth|cookie|"
    r"credential|private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)
_CONTENT_KEY = re.compile(
    r"(?:^|_)(?:raw|sample|content|body|blob|binary|bytes)(?:$|_)",
    re.IGNORECASE,
)
_PATH_KEY = re.compile(
    r"(?:^|_)(?:path|absolute_?path|source_?path|canonical_?path|filesystem_?path|"
    r"unc|device_?path|sensitive_?path)(?:$|_)",
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE = re.compile(r"(?i)(?:\\\\[?.]\\|\\\\[^\\\s]+\\[^\\\s]+|[A-Z]:[\\/])")
_WINDOWS_PROHIBITED = re.compile(
    r"(?i)(?:"
    r"(?<![A-Za-z0-9])[A-Z]:(?![\\/])[^\\\s]*(?:\\[^\\\s]+)+|"
    r"(?<![\\\w])\\(?![\\?.])[^\\\s]+(?:\\[^\\\s]+)*"
    r")"
)
_POSIX_ABSOLUTE = re.compile(r"(?<![:\w.])/(?!/)(?:[^/\s]+(?:/|$))+")
_SENSITIVE_SEGMENT = re.compile(
    r"(?i)(?:^|[\\/])(?:users?|home|\.ssh|private|confidential|secrets?)(?:[\\/]|$)"
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:"
    r"bearer\s+[A-Za-z0-9._~+/=-]{6,}|"
    r"(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{10,}|"
    r"(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{8,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"
    r")"
)
_SAFE_TYPE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True, slots=True)
class RedactionLimits:
    """Hard limits for recursive values and final JSON log size."""

    max_depth: int = 6
    max_items: int = 64
    max_string_length: int = 512
    max_log_bytes: int = 16_384

    def __post_init__(self) -> None:
        if not 1 <= self.max_depth <= 32:
            raise ValueError("max_depth must be between 1 and 32")
        if not 1 <= self.max_items <= 10_000:
            raise ValueError("max_items must be between 1 and 10000")
        if not 8 <= self.max_string_length <= 65_536:
            raise ValueError("max_string_length must be between 8 and 65536")
        if not 512 <= self.max_log_bytes <= 1_048_576:
            raise ValueError("max_log_bytes must be between 512 and 1048576")


class Redactor:
    """Convert arbitrary values into bounded JSON-safe redacted structures."""

    def __init__(self, limits: RedactionLimits | None = None) -> None:
        self.limits = limits or RedactionLimits()

    def redact(self, value: object, *, key: str | None = None) -> Any:
        """Redact one value recursively without invoking unknown representations."""

        return self._redact(value, key=key, depth=0)

    def _redact(self, value: object, *, key: str | None, depth: int) -> Any:
        if isinstance(value, (bytes, bytearray, memoryview)):
            return "[REDACTED:BYTES]"
        if key is not None:
            if _SECRET_KEY.search(key):
                return "[REDACTED:SECRET]"
            if _CONTENT_KEY.search(key):
                return "[REDACTED:CONTENT]"
            if _PATH_KEY.search(key):
                return "[REDACTED:PATH]"
        if isinstance(value, BaseException):
            return f"[REDACTED:EXCEPTION:{self._safe_type_name(type(value).__name__)}]"
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else "[REDACTED:NON_FINITE_NUMBER]"
        if isinstance(value, str):
            return self._redact_string(value)
        if isinstance(value, (UUID, date, datetime)):
            return self._redact_string(
                value.isoformat() if not isinstance(value, UUID) else str(value)
            )
        if isinstance(value, Enum):
            return self._redact(value.value, key=key, depth=depth)
        if isinstance(value, PurePath):
            return self._redact_string(value.as_posix())
        if depth >= self.limits.max_depth:
            return "[TRUNCATED:DEPTH]"
        if isinstance(value, BaseModel):
            return self._redact(value.model_dump(mode="python"), key=key, depth=depth)
        if is_dataclass(value) and not isinstance(value, type):
            safe_dataclass: dict[object, object] = {
                field.name: getattr(value, field.name) for field in fields(value)
            }
            return self._redact_mapping(safe_dataclass, depth=depth)
        if isinstance(value, Mapping):
            return self._redact_mapping(value, depth=depth)
        if isinstance(value, Sequence):
            return self._redact_sequence(value, depth=depth)
        return f"[REDACTED:OBJECT:{self._safe_type_name(type(value).__name__)}]"

    def _redact_mapping(self, value: Mapping[object, object], *, depth: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= self.limits.max_items:
                result["[TRUNCATED]"] = "[TRUNCATED:ITEMS]"
                break
            if isinstance(raw_key, str):
                safe_key = self._safe_key(raw_key, index=index)
            else:
                safe_key = f"[NON_STRING_KEY:{index}]"
            result[safe_key] = self._redact(item, key=safe_key, depth=depth + 1)
        return result

    def _redact_sequence(self, value: Sequence[object], *, depth: int) -> list[Any]:
        result = [
            self._redact(item, key=None, depth=depth + 1) for item in value[: self.limits.max_items]
        ]
        if len(value) > self.limits.max_items:
            result.append("[TRUNCATED:ITEMS]")
        return result

    def _safe_key(self, value: str, *, index: int) -> str:
        if _SECRET_KEY.search(value) or _SECRET_VALUE.search(value):
            return f"[REDACTED_KEY:SECRET:{index}]"
        if (
            _WINDOWS_ABSOLUTE.search(value)
            or _WINDOWS_PROHIBITED.search(value)
            or _POSIX_ABSOLUTE.search(value)
            or _SENSITIVE_SEGMENT.search(value)
        ):
            return f"[REDACTED_KEY:PATH:{index}]"
        if len(value) > 128:
            return f"[TRUNCATED:KEY:{index}]"
        return value

    def _redact_string(self, value: str) -> str:
        if (
            _SECRET_VALUE.search(value)
            or _WINDOWS_ABSOLUTE.search(value)
            or _WINDOWS_PROHIBITED.search(value)
            or _POSIX_ABSOLUTE.search(value)
            or _SENSITIVE_SEGMENT.search(value)
        ):
            marker = (
                "[REDACTED:PATH]"
                if (
                    _WINDOWS_ABSOLUTE.search(value)
                    or _WINDOWS_PROHIBITED.search(value)
                    or _POSIX_ABSOLUTE.search(value)
                    or _SENSITIVE_SEGMENT.search(value)
                )
                else "[REDACTED:SECRET]"
            )
            return marker
        if len(value) > self.limits.max_string_length:
            return f"{value[: self.limits.max_string_length]}...[TRUNCATED]"
        return value

    @staticmethod
    def _safe_type_name(value: str) -> str:
        sanitized = _SAFE_TYPE_NAME.sub("_", value)
        return sanitized[:64] or "Unknown"


__all__ = ["RedactionLimits", "Redactor"]
