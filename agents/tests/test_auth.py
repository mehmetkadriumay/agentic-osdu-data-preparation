import importlib.util
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp_servers.host.settings import Settings, principal_map


class PolicySettingsTests(unittest.TestCase):
    def test_principals_are_explicit_and_never_implicitly_approved(self):
        assert principal_map("{}") == {}
        principal = str(uuid4())
        assert principal_map(f'{{"{principal}": 6}}') == {principal: 6}
        for value in (
            "[]",
            f'{{"{principal}": true}}',
            f'{{"{principal}": 8}}',
            f'{{"{principal}": 1, "{principal}": 6}}',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                principal_map(value)

    def test_environment_rejects_traversal_and_wildcard_hosts(self):
        values = {
            "OSDU_DATABASE_URL": "postgresql+psycopg://user@localhost/osdu?sslmode=require",
            "OSDU_SOURCE_ROOT": str(Path.cwd().resolve()),
            "OSDU_SCHEMA_CATALOG_ROOT": str(Path.cwd().parent / "schema-cache"),
            "OSDU_TENANT_ID": str(uuid4()),
            "OSDU_API_AUDIENCE": str(uuid4()),
            "OSDU_ALLOWED_HOSTS": '["backend.example.com"]',
        }
        with patch.dict("os.environ", values, clear=True):
            assert Settings.from_env().agents == {}
            for name, value in (
                ("OSDU_SCAN_SUBPATH", "../Other"),
                ("OSDU_ALLOWED_HOSTS", '["*.example.com"]'),
                ("OSDU_SCHEMA_CATALOG_ROOT", str(Path.cwd() / "schemas")),
                ("OSDU_DATABASE_URL", "postgresql+psycopg://user@localhost/osdu"),
            ):
                with (
                    self.subTest(name=name),
                    patch.dict("os.environ", {name: value}),
                    self.assertRaises(ValueError),
                ):
                    Settings.from_env()


@unittest.skipUnless(
    importlib.util.find_spec("jwt") and importlib.util.find_spec("cryptography"),
    "JWT tests require backend requirements",
)
class EntraAuthenticationTests(unittest.TestCase):
    def setUp(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from mcp_servers.host.auth import AccessDenied, EntraAuthenticator

        self.jwt = jwt
        self.denied = AccessDenied
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        tenant = str(uuid4())
        audience = str(uuid4())
        self.principal = str(uuid4())
        settings = Settings(
            database_url="sqlite://",
            source_root=Path("/source"),
            scan_subpath="Data",
            tenant_id=tenant,
            audience=audience,
            agents={self.principal: 6},
            readers=frozenset(),
            allowed_hosts=frozenset({"backend.example.com"}),
            schema_catalog_root=Path("/cache"),
            max_files=100,
        )
        self.auth = EntraAuthenticator(settings)
        self.auth.keys = Mock()
        self.auth.keys.get_signing_key_from_jwt.return_value = SimpleNamespace(
            key=self.private_key.public_key()
        )
        now = int(time.time())
        self.claims = {
            "iss": f"https://login.microsoftonline.com/{tenant}/v2.0",
            "aud": audience,
            "tid": tenant,
            "oid": self.principal,
            "iat": now - 1,
            "nbf": now - 1,
            "exp": now + 300,
        }

    def token(self, **overrides):
        claims = self.claims | overrides
        return "Bearer " + self.jwt.encode(claims, self.private_key, algorithm="RS256")

    def test_valid_token_binds_the_exact_capability_role_not_the_claimed_actor(self):
        caller = self.auth.authenticate(self.token())
        assert caller.principal_id == self.principal
        assert caller.role == 6
        assert not caller.reader

    def test_wrong_audience_tenant_expiry_and_unknown_principal_are_denied(self):
        for override in (
            {"aud": str(uuid4())},
            {"tid": str(uuid4())},
            {"iss": "https://attacker.example"},
            {"exp": int(time.time()) - 120},
            {"oid": str(uuid4())},
        ):
            with self.subTest(override=override), self.assertRaises(self.denied):
                self.auth.authenticate(self.token(**override))
        with self.assertRaises(self.denied):
            self.auth.authenticate("")


if __name__ == "__main__":
    unittest.main()
