from __future__ import annotations

import asyncio
import configparser
import contextlib
import importlib
import importlib.util
import signal
import subprocess
import sys
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import dev
import preflight
from optional_features import feature_enabled, initial_extensions, optional_webhook


def load_ipc_client_module():
    """Load the IPC client without importing runtime configuration or secrets."""
    package = types.ModuleType("hardening_meta")
    package.__path__ = []
    ipc_package = types.ModuleType("hardening_meta.ipc")
    ipc_package.__path__ = []
    logger_module = types.ModuleType("hardening_meta.logger")
    logger_module.logging_context = lambda **_kwargs: contextlib.nullcontext()
    logger_module.set_logging_context = lambda **_kwargs: None
    logger_module.log_wrap = lambda **_kwargs: (lambda function: function)

    module_name = "hardening_meta.ipc.client"
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "src/meta/ipc/client.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        sys.modules,
        {
            "hardening_meta": package,
            "hardening_meta.ipc": ipc_package,
            "hardening_meta.logger": logger_module,
            module_name: module,
        },
    ):
        spec.loader.exec_module(module)
    return module


class ConfigHardeningTests(unittest.TestCase):
    def test_example_config_interpolation_and_safe_defaults(self):
        parser = preflight.load_configuration(
            ROOT / "config/example-bot.conf",
            follow_includes=False,
        )
        self.assertEqual(parser.get("LOGGING", "error_log"), parser.get("LOGGING", "general_log"))
        self.assertFalse(feature_enabled(parser, "ANALYTICS"))
        self.assertFalse(feature_enabled(parser, "PREMIUM"))
        self.assertFalse(feature_enabled(parser, "TOPGG"))
        self.assertFalse(parser.getboolean("GO_STUDY", "notifications_enabled"))
        self.assertNotIn("analytics", initial_extensions(parser))

        modules = importlib.import_module("modules")
        configured = modules.configured_extensions(parser)
        self.assertNotIn(".premium", configured)
        self.assertNotIn(".topgg", configured)

    def test_optional_features_default_to_disabled_when_section_is_absent(self):
        parser = configparser.ConfigParser()
        self.assertFalse(feature_enabled(parser, "PREMIUM"))

    def test_test_and_premium_modules_are_excluded_by_default(self):
        modules = importlib.import_module("modules")
        self.assertNotIn(".test", modules.active)
        self.assertNotIn(".premium", modules.active)
        self.assertNotIn(".topgg", modules.active)

    def test_skin_paths_guard_missing_premium(self):
        source = (ROOT / "src/modules/skins/cog.py").read_text(encoding="utf-8")
        self.assertIn("Premium is disabled; using the default guild skin.", source)
        self.assertIn("if self.bot.get_cog('PremiumCog') is None:", source)
        self.assertIn("if premiumcog is None:", source)


class PremiumWebhookTests(unittest.TestCase):
    def test_blank_webhook_is_harmless(self):
        factory = mock.Mock()
        logger = mock.Mock()
        self.assertIsNone(optional_webhook("  ", factory, session=object(), logger=logger))
        factory.assert_not_called()
        logger.warning.assert_not_called()

    def test_malformed_webhook_is_harmless_and_redacted(self):
        secret_url = "https://example.invalid/private-webhook-value"
        factory = mock.Mock(side_effect=ValueError(secret_url))
        logger = mock.Mock()
        self.assertIsNone(optional_webhook(secret_url, factory, session=object(), logger=logger))
        logged = " ".join(str(value) for call in logger.warning.call_args_list for value in call.args)
        self.assertNotIn(secret_url, logged)
        self.assertIn("invalid", logged.lower())


class PreflightTests(unittest.TestCase):
    def test_preflight_database_failure_does_not_print_connection_details(self):
        secret = "password=never-print-this"
        parser = configparser.ConfigParser()
        parser.read_dict({"DATA": {"args": secret}})
        output = []

        fake_psycopg = SimpleNamespace(connect=mock.Mock(side_effect=RuntimeError(secret)))
        with mock.patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            preflight.check_database(preflight.Reporter(output.append), parser)

        self.assertNotIn(secret, "\n".join(output))

    def test_latest_schema_version(self):
        cursor = mock.MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = (19,)
        connection = mock.Mock()
        connection.cursor.return_value = cursor
        self.assertEqual(preflight.latest_schema_version(connection), 19)
        self.assertTrue(preflight.schema_version_is_current(19))
        self.assertFalse(preflight.schema_version_is_current(18))

    def test_schema_declares_version_19(self):
        schema = (ROOT / "data/schema.sql").read_text(encoding="utf-8")
        self.assertIn("INSERT INTO VersionHistory (version, author) VALUES (19,", schema)


class SupervisorTests(unittest.TestCase):
    def test_child_failure_returns_nonzero(self):
        registry = mock.Mock()
        registry.poll.return_value = None
        bot = mock.Mock()
        bot.poll.return_value = 7
        status = dev.monitor_children(registry, bot, threading.Event())
        self.assertEqual(status, 7)

    def test_shutdown_orders_bot_before_registry(self):
        order = []
        with mock.patch.object(dev, "stop_child", side_effect=lambda process: order.append(process)):
            dev.shutdown_children("bot", "registry")
        self.assertEqual(order, ["bot", "registry"])

    def test_stop_child_uses_sigint_for_graceful_shutdown(self):
        process = mock.Mock()
        process.poll.return_value = None
        dev.stop_child(process)
        process.send_signal.assert_called_once_with(signal.SIGINT)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_stop_child_escalates_after_bounded_timeouts(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [
            subprocess.TimeoutExpired("bot", 0),
            subprocess.TimeoutExpired("bot", 0),
            0,
        ]
        dev.stop_child(process, timeout=0)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()


class IpcShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_cancels_tasks_closes_sockets_and_prevents_reconnect(self):
        ipc_client = load_ipc_client_module()
        client = ipc_client.AppClient(
            "GoStudy_01_00",
            "GoStudy",
            {"host": "127.0.0.1", "port": 5001},
            {"host": "127.0.0.1", "port": 5000},
        )
        keepalive = asyncio.create_task(asyncio.sleep(60))
        reconnect = asyncio.create_task(asyncio.sleep(60))
        writer = mock.Mock()
        writer.wait_closed = mock.AsyncMock()
        listener = mock.Mock()
        listener.wait_closed = mock.AsyncMock()
        client._keepalive = keepalive
        client._reconnect_task = reconnect
        client._server = (object(), writer)
        client._listener = listener

        await client.close()
        await client.close()

        self.assertTrue(client._closing)
        self.assertTrue(keepalive.cancelled())
        self.assertTrue(reconnect.cancelled())
        writer.close.assert_called_once_with()
        writer.wait_closed.assert_awaited_once_with()
        listener.close.assert_called_once_with()
        listener.wait_closed.assert_awaited_once_with()
        with self.assertRaisesRegex(RuntimeError, "closed AppClient"):
            await client.connect()


class RequirementsTests(unittest.TestCase):
    def test_requirements_are_fully_pinned_without_binary_psycopg(self):
        lines = [
            line.strip()
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertTrue(lines)
        self.assertTrue(all("==" in line for line in lines))
        self.assertFalse(any(line.lower().startswith("psycopg-binary") for line in lines))
        self.assertIn("psycopg[pool]==3.1.18", lines)
        self.assertIn("psycopg-pool==3.3.1", lines)


if __name__ == "__main__":
    unittest.main()
