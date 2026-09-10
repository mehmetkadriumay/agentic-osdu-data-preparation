"""JSON-first CLI for the EPIC-009 local API."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Never
from urllib.parse import urlsplit

from agentic_osdu.api.server import ServerSettings

Transport = Callable[[str, dict[str, object]], tuple[int, dict[str, object]]]

_ROUTES = {
    "discover": "/api/v1/inventories/discover",
    "classify": "/api/v1/inventories/classify",
    "manifest-match": "/api/v1/associations/match",
    "manifest-learn": "/api/v1/learning/learn",
    "manifest-generate": "/api/v1/generation/one",
    "manifest-generate-all": "/api/v1/generation/all",
    "manifest-validate": "/api/v1/validation",
    "job-status": "/api/v1/jobs/events",
    "job-cancel": "/api/v1/jobs/cancel",
    "review-export": "/api/v1/exports",
}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="agentic-osdu")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in _ROUTES:
        command = commands.add_parser(name)
        command.add_argument("--json", required=True, dest="json_payload")
    serve = commands.add_parser("web-serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    serve.add_argument("--allow-non-loopback", action="store_true")
    migration = commands.add_parser("migration")
    migration.add_argument("--inventory", required=True)
    migration.add_argument("--learning", required=True)
    migration.add_argument("--database", required=True)
    migration.add_argument("--legacy-root", required=True)
    return parser


def _http_transport(base_url: str) -> Transport:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("API URL must be an HTTP(S) URL without embedded credentials")

    def send(route: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}{route}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    return send


def main(argv: Sequence[str] | None = None, *, transport: Transport | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except (ValueError, SystemExit) as error:
        print(json.dumps({"errors": [{"code": "CLI_USAGE_ERROR", "message": str(error)}]}))
        return 2
    if args.command == "web-serve":
        try:
            settings = ServerSettings(
                host=args.host,
                port=args.port,
                allow_non_loopback=args.allow_non_loopback,
            )
        except ValueError as error:
            print(json.dumps({"errors": [{"code": "BINDING_DENIED", "message": str(error)}]}))
            return 2
        import uvicorn

        uvicorn.run(
            "agentic_osdu.api.runtime:app",
            host=settings.host,
            port=settings.port,
        )
        return 0
    if args.command == "migration":
        from agentic_osdu.migration import LegacyMigrationError, migrate_to_sqlite

        database_path = Path(args.database).resolve()
        try:
            report = migrate_to_sqlite(
                Path(args.inventory),
                Path(args.learning),
                database_path,
                legacy_root=Path(args.legacy_root),
            )
        except LegacyMigrationError as error:
            print(json.dumps({"errors": [{"code": error.code, "message": str(error)}]}))
            return 3
        print(json.dumps(report.model_dump(mode="json"), sort_keys=True))
        return 0
    try:
        payload = json.loads(args.json_payload)
    except json.JSONDecodeError:
        print(json.dumps({"errors": [{"code": "CLI_JSON_INVALID", "message": "Invalid JSON."}]}))
        return 2
    if not isinstance(payload, dict):
        print(
            json.dumps({"errors": [{"code": "CLI_JSON_INVALID", "message": "Use a JSON object."}]})
        )
        return 2
    try:
        status, response = (transport or _http_transport(args.api_url))(
            _ROUTES[args.command], payload
        )
    except (ValueError, OSError, urllib.error.URLError):
        print(
            json.dumps(
                {
                    "errors": [
                        {
                            "code": "API_UNAVAILABLE",
                            "message": "The configured local API is unavailable.",
                        }
                    ]
                }
            )
        )
        return 4
    print(json.dumps(response, sort_keys=True))
    if 200 <= status < 300 and response.get("status") not in {
        "failed",
        "cancelled",
        "partially_succeeded",
    }:
        return 0
    if 200 <= status < 300:
        return 3
    return 3 if 400 <= status < 500 else 4


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
