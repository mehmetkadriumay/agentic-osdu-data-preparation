"""Validate Entra access tokens before interpreting any workflow input."""

from dataclasses import dataclass
from uuid import UUID

import jwt

from .settings import Settings


class AccessDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class Caller:
    principal_id: str
    stage: int | None
    reader: bool


class EntraAuthenticator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.issuer = f"https://login.microsoftonline.com/{settings.tenant_id}/v2.0"
        self.keys = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{settings.tenant_id}/discovery/v2.0/keys",
            cache_jwk_set=True,
            lifespan=300,
            timeout=10,
        )

    def authenticate(self, authorization: str) -> Caller:
        parts = authorization.split()
        if len(parts) != 2 or parts[0].casefold() != "bearer" or len(parts[1]) > 16_384:
            raise AccessDenied("A bearer access token is required")
        try:
            signing_key = self.keys.get_signing_key_from_jwt(parts[1])
            claims = jwt.decode(
                parts[1],
                signing_key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.settings.audience,
                leeway=30,
                options={"require": ["exp", "iat", "nbf", "iss", "aud", "tid", "oid"]},
            )
            principal = str(UUID(claims["oid"]))
            if claims["tid"] != self.settings.tenant_id:
                raise ValueError("Unexpected token tenant")
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as error:
            raise AccessDenied("The access token could not be validated") from error
        stage = self.settings.agents.get(principal)
        reader = principal in self.settings.readers
        if stage is None and not reader:
            raise AccessDenied("This principal is not authorized")
        return Caller(principal_id=principal, stage=stage, reader=reader)
