"""Loopback-only server configuration."""

from __future__ import annotations

import ipaddress

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ServerSettings(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    allow_non_loopback: bool = False

    @model_validator(mode="after")
    def require_explicit_non_loopback(self) -> ServerSettings:
        host = self.host.casefold()
        loopback = host == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
        if not loopback and not self.allow_non_loopback:
            raise ValueError("non-loopback binding requires explicit configuration")
        return self


__all__ = ["ServerSettings"]
