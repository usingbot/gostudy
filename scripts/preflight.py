#!/usr/bin/env python3
"""Validate a Go Study development environment without printing secrets."""

from __future__ import annotations

import argparse
import configparser
import importlib.metadata
import socket
import sys
from pathlib import Path
from typing import Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SCHEMA_VERSION = 16
EXPECTED_PYTHON = (3, 11, 16)
PLACEHOLDERS = ("CHANGE_ME", "REPLACE_WITH_")
EXPECTED_DEPENDENCIES = {
    "aiohttp": "3.7.4.post0",
    "async-timeout": "3.0.1",
    "attrs": "26.1.0",
    "bidict": "0.24.1",
    "cachetools": "4.2.2",
    "cffi": "2.1.1",
    "chardet": "4.0.0",
    "configparser": "5.0.2",
    "davey": "0.1.6",
    "discord.py": "2.7.1",
    "frozendict": "2.4.7",
    "idna": "3.19",
    "iso8601": "0.1.16",
    "multidict": "6.7.1",
    "Pillow": "12.3.0",
    "propcache": "0.5.2",
    "psutil": "7.2.2",
    "psycopg": "3.1.18",
    "psycopg-pool": "3.3.1",
    "pycparser": "3.0",
    "PyNaCl": "1.5.0",
    "python-dateutil": "2.9.0.post0",
    "pytz": "2021.1",
    "six": "1.17.0",
    "topggpy": "1.4.0",
    "typing_extensions": "4.16.0",
    "yarl": "1.24.5",
}


class Reporter:
    def __init__(self, output: Callable[[str], None] = print):
        self.output = output
        self.failures = 0

    def ok(self, message: str) -> None:
        self.output(f"[OK] {message}")

    def warn(self, message: str) -> None:
        self.output(f"[WARN] {message}")

    def fail(self, message: str) -> None:
        self.failures += 1
        self.output(f"[FAIL] {message}")


def resolve_repo_path(value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def _read_config_file(parser: configparser.ConfigParser, path: Path) -> None:
    with path.open(encoding="utf-8") as stream:
        parser.read_file(stream)


def load_configuration(path: Path, *, follow_includes: bool) -> configparser.ConfigParser:
    """Load configuration while resolving includes relative to the repository."""
    parser = configparser.ConfigParser()
    _read_config_file(parser, path)

    if follow_includes:
        pending = [
            item.strip()
            for item in parser.get("STUDYLION", "ALSO_READ", fallback="").split(",")
            if item.strip()
        ]
        loaded = {path.resolve()}
        while pending:
            include = resolve_repo_path(pending.pop(0))
            if include in loaded:
                continue
            if not include.is_file():
                raise FileNotFoundError
            _read_config_file(parser, include)
            loaded.add(include)
            for item in parser.get("STUDYLION", "ALSO_READ", fallback="").split(","):
                item = item.strip()
                if item:
                    candidate = resolve_repo_path(item)
                    if candidate not in loaded and item not in pending:
                        pending.append(item)

    # Force interpolation now so malformed templates fail before startup.
    for section in parser.sections():
        for option in parser.options(section):
            parser.get(section, option)
    return parser


def configured_feature(parser: configparser.ConfigParser, section: str) -> bool:
    return parser.getboolean(section, "enabled", fallback=False)


def valid_secret(value: str) -> bool:
    normalized = value.strip()
    return bool(normalized) and not normalized.upper().startswith(PLACEHOLDERS)


def valid_port(value: object) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        return True
    except OSError:
        return False


def latest_schema_version(connection) -> int | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1")
        row = cursor.fetchone()
    return int(row[0]) if row else None


def schema_version_is_current(version: int | None) -> bool:
    return version == EXPECTED_SCHEMA_VERSION


def check_python(reporter: Reporter) -> None:
    actual = sys.version_info[:3]
    if actual[:2] != EXPECTED_PYTHON[:2]:
        reporter.fail(f"Python 3.11 is required; found {actual[0]}.{actual[1]}.{actual[2]}.")
    elif actual != EXPECTED_PYTHON:
        reporter.warn(
            f"Python {actual[0]}.{actual[1]}.{actual[2]} is compatible, "
            "but .python-version selects 3.11.16."
        )
    else:
        reporter.ok("Python 3.11.16 is active.")


def check_dependencies(reporter: Reporter) -> None:
    mismatches = []
    for distribution, expected in EXPECTED_DEPENDENCIES.items():
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            mismatches.append(f"{distribution} is missing")
        else:
            if actual != expected:
                mismatches.append(f"{distribution} must be {expected} (found {actual})")

    if mismatches:
        for mismatch in mismatches:
            reporter.fail(mismatch)
    else:
        reporter.ok("All locked Python dependencies match requirements.txt.")

    try:
        from psycopg._encodings import pgconn_encoding  # noqa: F401
    except Exception:
        reporter.fail("psycopg 3.1 compatibility import is unavailable.")
    else:
        reporter.ok("psycopg private encoding compatibility import is available.")

    try:
        importlib.metadata.version("psycopg-binary")
    except importlib.metadata.PackageNotFoundError:
        reporter.ok("psycopg-binary is not installed.")
    else:
        reporter.fail("psycopg-binary must not be installed in the development environment.")


def check_submodules(reporter: Reporter) -> None:
    sentinels = (REPO_ROOT / "src/gui/__init__.py", REPO_ROOT / "skins/skins.json")
    if all(path.is_file() for path in sentinels):
        reporter.ok("GUI and skin submodules are initialized.")
    else:
        reporter.fail("Git submodules are missing; run git submodule update --init --recursive.")


def check_example_config(reporter: Reporter) -> None:
    try:
        for name in ("example-bot.conf", "example-secrets.conf", "example-gui.conf"):
            load_configuration(REPO_ROOT / "config" / name, follow_includes=False)
    except (OSError, configparser.Error, ValueError):
        reporter.fail("One or more tracked example configurations do not parse cleanly.")
    else:
        reporter.ok("Tracked example configurations parse cleanly.")


def check_runtime_config(reporter: Reporter, config_path: Path):
    try:
        parser = load_configuration(config_path, follow_includes=True)
    except (OSError, configparser.Error, ValueError):
        reporter.fail("Runtime configuration is missing or invalid.")
        return None

    reporter.ok("Runtime configuration and includes parse cleanly.")

    token = parser.get("STUDYLION", "token", fallback="")
    if valid_secret(token):
        reporter.ok("Discord bot token is configured.")
    else:
        reporter.fail("Discord bot token is empty or still a placeholder.")

    data_args = parser.get("DATA", "args", fallback="").strip()
    appid = parser.get("DATA", "appid", fallback="").strip()
    if data_args and appid:
        reporter.ok("Database connection settings and application ID are configured.")
    else:
        reporter.fail("Database connection settings or application ID are empty.")

    try:
        analytics = configured_feature(parser, "ANALYTICS")
        premium = configured_feature(parser, "PREMIUM")
        topgg = configured_feature(parser, "TOPGG")
    except ValueError:
        reporter.fail("Optional feature flags must be true or false.")
    else:
        reporter.ok(
            "Optional features: "
            f"analytics={'enabled' if analytics else 'disabled'}, "
            f"premium={'enabled' if premium else 'disabled'}, "
            f"topgg={'enabled' if topgg else 'disabled'}."
        )
    return parser


def check_ports(
    reporter: Reporter,
    parser: configparser.ConfigParser,
    bot_host: str,
    bot_port_value: object,
) -> None:
    registry_host = parser.get("APPIPC", "server_host", fallback="127.0.0.1").strip()
    registry_port = valid_port(parser.get("APPIPC", "server_port", fallback=""))
    bot_port = valid_port(bot_port_value)
    if registry_port is None or bot_port is None:
        reporter.fail("Registry and bot listener ports must be integers from 1 through 65535.")
        return
    if registry_host == bot_host and registry_port == bot_port:
        reporter.fail("Registry and bot listener ports must be different.")
        return
    if not port_available(registry_host, registry_port):
        reporter.fail("The configured registry port is already in use or cannot be bound.")
    elif not port_available(bot_host, bot_port):
        reporter.fail("The configured bot listener port is already in use or cannot be bound.")
    else:
        reporter.ok("Registry and bot listener ports are valid and available.")


def check_database(reporter: Reporter, parser: configparser.ConfigParser) -> None:
    try:
        import psycopg

        connection_args = parser.get("DATA", "args", fallback="").strip()
        with psycopg.connect(connection_args, connect_timeout=5) as connection:
            version = latest_schema_version(connection)
    except Exception:
        reporter.fail("PostgreSQL is unreachable or schema validation failed.")
        return

    reporter.ok("PostgreSQL is reachable.")
    if schema_version_is_current(version):
        reporter.ok(f"Database schema version is {EXPECTED_SCHEMA_VERSION}.")
    else:
        reporter.fail(
            f"Database schema version must be {EXPECTED_SCHEMA_VERSION}; "
            "inspect VersionHistory before starting the bot."
        )


def run_preflight(
    config_path: Path,
    *,
    bot_host: str,
    bot_port: object,
    check_database_connection: bool = True,
    output: Callable[[str], None] = print,
) -> int:
    reporter = Reporter(output)
    check_python(reporter)
    check_dependencies(reporter)
    check_submodules(reporter)
    check_example_config(reporter)
    parser = check_runtime_config(reporter, config_path)
    if parser is not None:
        check_ports(reporter, parser, bot_host, bot_port)
        if check_database_connection:
            check_database(reporter, parser)

    if reporter.failures:
        output(f"Preflight failed with {reporter.failures} blocking issue(s).")
        return 1
    output("Preflight passed.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/bot.conf", help="Runtime bot configuration path.")
    parser.add_argument("--bot-host", default="127.0.0.1", help="Bot IPC listener host.")
    parser.add_argument("--bot-port", default="5001", help="Bot IPC listener port.")
    parser.add_argument(
        "--skip-database",
        action="store_true",
        help="Skip database connectivity only when diagnosing non-database setup.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run_preflight(
        resolve_repo_path(args.config),
        bot_host=args.bot_host,
        bot_port=args.bot_port,
        check_database_connection=not args.skip_database,
    )


if __name__ == "__main__":
    raise SystemExit(main())
