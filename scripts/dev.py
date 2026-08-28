#!/usr/bin/env python3
"""Run the Go Study registry and bot as one local development process."""

from __future__ import annotations

import argparse
import pickle
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from preflight import REPO_ROOT, load_configuration, resolve_repo_path, valid_port


SHUTDOWN_TIMEOUT_SECONDS = 10
REGISTRY_START_TIMEOUT_SECONDS = 15


def stop_child(process: subprocess.Popen | None, *, timeout: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
    """Ask a child to stop, then bound escalation to terminate and kill."""
    if process is None or process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        try:
            process.terminate()
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print("A child process did not exit after the bounded shutdown sequence.", file=sys.stderr)


def wait_for_registry(
    host: str,
    port: int,
    process: subprocess.Popen,
    *,
    timeout: float = REGISTRY_START_TIMEOUT_SECONDS,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection((host, port), timeout=0.25) as connection:
                connection.sendall(pickle.dumps(("ping", (), {})) + b"\n")
                if connection.recv(16) == b"Pong":
                    return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def monitor_children(
    registry: subprocess.Popen,
    bot: subprocess.Popen,
    stop_event: threading.Event,
) -> int:
    while not stop_event.wait(0.2):
        for name, process in (("registry", registry), ("bot", bot)):
            returncode = process.poll()
            if returncode is not None:
                print(f"{name.capitalize()} exited unexpectedly with status {returncode}.", file=sys.stderr)
                return returncode if returncode != 0 else 1
    return 0


def shutdown_children(bot: subprocess.Popen | None, registry: subprocess.Popen | None) -> None:
    """Preserve shutdown order: Discord bot first, registry second."""
    stop_child(bot)
    stop_child(registry)


def run_preflight(config_path: Path, bot_host: str, bot_port: int) -> int:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts/preflight.py"),
        "--config",
        str(config_path),
        "--bot-host",
        bot_host,
        "--bot-port",
        str(bot_port),
    ]
    return subprocess.run(command, cwd=REPO_ROOT).returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/bot.conf", help="Runtime bot configuration path.")
    parser.add_argument("--shard", type=int, default=0, help="Discord shard number.")
    parser.add_argument("--host", default="127.0.0.1", help="Bot IPC listener host.")
    parser.add_argument("--port", type=int, default=5001, help="Bot IPC listener port.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = resolve_repo_path(args.config)
    bot_port = valid_port(args.port)
    if bot_port is None:
        print("Bot IPC port must be an integer from 1 through 65535.", file=sys.stderr)
        return 2

    preflight_status = run_preflight(config_path, args.host, bot_port)
    if preflight_status:
        return preflight_status

    parser = load_configuration(config_path, follow_includes=True)
    registry_host = parser.get("APPIPC", "server_host").strip()
    registry_port = int(parser.get("APPIPC", "server_port"))

    stop_event = threading.Event()

    def request_shutdown(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    registry = None
    bot = None
    try:
        registry = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "scripts/start_registry.py"), "--conf", str(config_path)],
            cwd=REPO_ROOT,
            start_new_session=True,
        )
        if not wait_for_registry(registry_host, registry_port, registry):
            print("Registry did not become ready before the startup deadline.", file=sys.stderr)
            return 1

        bot = subprocess.Popen(
            [
                sys.executable,
                str(REPO_ROOT / "scripts/start_leo.py"),
                "--conf",
                str(config_path),
                "--shard",
                str(args.shard),
                "--host",
                args.host,
                "--port",
                str(bot_port),
            ],
            cwd=REPO_ROOT,
            start_new_session=True,
        )
        print("Registry and Go Study bot started. Press Ctrl-C to stop.")
        return monitor_children(registry, bot, stop_event)
    except KeyboardInterrupt:
        return 0
    finally:
        shutdown_children(bot, registry)


if __name__ == "__main__":
    raise SystemExit(main())
