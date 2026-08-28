from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import configparser
import importlib
import logging
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest import mock
from uuid import uuid4

import discord
from discord.ext import commands


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))

import preflight
from modules.gostudy_rewards.data import GoStudyRewardsData
from modules.gostudy_rewards.notifications import (
    DiscordFailure,
    FALLBACK_DISPLAY_NAME,
    RewardNotificationWorker,
    classify_discord_failure,
    format_reward_message,
    inventory_link_view,
    inventory_url_from_base,
    merge_claim_details,
    safe_display_name,
)


def discord_error(error_type, status: int, secret_body: str = 'private response body'):
    response = SimpleNamespace(status=status, reason='test reason')
    return error_type(response, {'code': 0, 'message': secret_body})


def claim_rows(*, attempt_count: int = 1):
    return [
        {
            'notificationid': 9_007_199_254_740_993,
            'hour_rewardid': 9_007_199_254_740_995,
            'userid': 9_007_199_254_740_997,
            'attempt_count': attempt_count,
        },
        {
            'notificationid': 9_007_199_254_740_999,
            'hour_rewardid': 9_007_199_254_741_001,
            'userid': 9_007_199_254_740_997,
            'attempt_count': attempt_count,
        },
    ]


def fake_worker_dependencies(*, send_side_effect=None, details=None):
    claimed = claim_rows()
    if details is None:
        details = [
            {
                **claimed[0],
                'item_key': 'coffee',
                'display_name': 'Coffee',
            },
            {
                **claimed[1],
                'item_key': 'coffee',
                'display_name': 'Coffee',
            },
        ]

    data = SimpleNamespace(
        claim_notification_batch=mock.AsyncMock(return_value=claimed),
        fetch_notification_claim_details=mock.AsyncMock(return_value=details),
        mark_notification_claim_delivered=mock.AsyncMock(return_value=2),
        retry_notification_claim=mock.AsyncMock(return_value=2),
        fail_notification_claim=mock.AsyncMock(return_value=2),
    )
    user = SimpleNamespace(send=mock.AsyncMock(side_effect=send_side_effect))
    bot = SimpleNamespace(
        wait_until_ready=mock.AsyncMock(),
        get_user=mock.Mock(return_value=user),
        fetch_user=mock.AsyncMock(return_value=user),
    )
    log = mock.Mock(spec=logging.Logger)
    worker = RewardNotificationWorker(
        bot,
        data,
        claimed_by='GoStudy_01_00',
        inventory_url='https://study.example/inventory',
        log=log,
    )
    return worker, bot, user, data, log


class NotificationPresentationTests(unittest.TestCase):
    def test_blank_web_url_has_no_button(self):
        self.assertIsNone(inventory_url_from_base(''))
        self.assertIsNone(inventory_link_view(None))

    def test_valid_web_url_appends_exact_inventory_path(self):
        url = inventory_url_from_base('https://study.example/base///')
        self.assertEqual(url, 'https://study.example/base/inventory')
        view = inventory_link_view(url)
        self.assertIsNotNone(view)
        self.assertEqual(len(view.children), 1)
        self.assertEqual(view.children[0].label, 'View Inventory')
        self.assertEqual(view.children[0].url, url)
        self.assertIsNone(view.children[0].custom_id)

    def test_invalid_web_url_is_omitted_and_not_reported(self):
        secret_url = 'javascript://private-secret-value'
        self.assertIsNone(inventory_url_from_base(secret_url))
        parser = configparser.ConfigParser()
        parser.read_dict({
            'STUDYLION': {'token': 'configured'},
            'DATA': {'args': 'configured', 'appid': 'GoStudy'},
            'ANALYTICS': {'enabled': 'false'},
            'PREMIUM': {'enabled': 'false'},
            'TOPGG': {'enabled': 'false'},
            'GO_STUDY': {
                'notifications_enabled': 'true',
                'web_url': secret_url,
            },
        })
        output = []
        with mock.patch.object(preflight, 'load_configuration', return_value=parser):
            preflight.check_runtime_config(
                preflight.Reporter(output.append),
                ROOT / 'unused',
            )

        self.assertFalse(preflight.web_inventory_link_configured(secret_url))
        self.assertNotIn(secret_url, '\n'.join(output))
        self.assertIn('web inventory link = not configured.', '\n'.join(output))

    def test_names_are_escaped_bounded_and_have_safe_fallback(self):
        escaped = safe_display_name('*' * 200 + '@everyone')
        self.assertLessEqual(len(escaped), 80)
        self.assertIn('\\*', escaped)
        self.assertEqual(safe_display_name(None), FALLBACK_DISPLAY_NAME)
        self.assertEqual(safe_display_name(' \n '), FALLBACK_DISPLAY_NAME)

    def test_multiple_rewards_aggregate_duplicates_without_losing_ids(self):
        claimed = claim_rows()
        merged = merge_claim_details(claimed, [
            {**claimed[0], 'item_key': 'coffee', 'display_name': 'Coffee'},
            {**claimed[1], 'item_key': 'coffee', 'display_name': 'Coffee'},
        ])
        self.assertEqual(
            [row['notificationid'] for row in merged],
            [9_007_199_254_740_993, 9_007_199_254_740_999],
        )
        message = format_reward_message(merged)
        self.assertIn('🎁 2 rewards earned!', message)
        self.assertIn('☕ Coffee ×2', message)
        self.assertIn('camera is on in supported study rooms', message)
        self.assertNotIn('active study', message)

    def test_missing_inventory_metadata_uses_safe_fallback(self):
        claimed = claim_rows()[:1]
        merged = merge_claim_details(claimed, [])
        message = format_reward_message(merged)
        self.assertEqual(merged[0]['notificationid'], claimed[0]['notificationid'])
        self.assertIn(f'🎁 {FALLBACK_DISPLAY_NAME}', message)


class DiscordFailureTests(unittest.TestCase):
    def test_terminal_discord_failures(self):
        forbidden = classify_discord_failure(discord_error(discord.Forbidden, 403))
        not_found = classify_discord_failure(discord_error(discord.NotFound, 404))
        other_4xx = classify_discord_failure(discord_error(discord.HTTPException, 400))
        self.assertEqual(forbidden, DiscordFailure('dm_forbidden', terminal=True))
        self.assertEqual(not_found, DiscordFailure('user_not_found', terminal=True))
        self.assertEqual(other_4xx, DiscordFailure('discord_http_4xx', terminal=True))

    def test_retryable_discord_and_network_failures(self):
        rate_limited = classify_discord_failure(
            discord_error(discord.HTTPException, 429)
        )
        server_error = classify_discord_failure(
            discord_error(discord.HTTPException, 503)
        )
        network_error = classify_discord_failure(OSError('private network body'))
        self.assertEqual(
            rate_limited,
            DiscordFailure('discord_rate_limited', terminal=False),
        )
        self.assertEqual(
            server_error,
            DiscordFailure('discord_server_error', terminal=False),
        )
        self.assertEqual(network_error, DiscordFailure('network_error', terminal=False))


class NotificationWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_dm_marks_entire_claim_delivered(self):
        worker, bot, user, data, _log = fake_worker_dependencies()
        self.assertTrue(await worker.process_once())
        user.send.assert_awaited_once()
        kwargs = user.send.await_args.kwargs
        self.assertEqual(
            kwargs['allowed_mentions'].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )
        self.assertEqual(len(kwargs['view'].children), 1)
        data.mark_notification_claim_delivered.assert_awaited_once()
        data.retry_notification_claim.assert_not_awaited()
        data.fail_notification_claim.assert_not_awaited()
        bot.fetch_user.assert_not_awaited()

    async def test_forbidden_and_not_found_are_terminal(self):
        for error, expected_code in (
            (discord_error(discord.Forbidden, 403), 'dm_forbidden'),
            (discord_error(discord.NotFound, 404), 'user_not_found'),
        ):
            with self.subTest(expected_code=expected_code):
                worker, _bot, _user, data, _log = fake_worker_dependencies(
                    send_side_effect=error,
                )
                await worker.process_once()
                self.assertEqual(
                    data.fail_notification_claim.await_args.args[1],
                    expected_code,
                )
                data.retry_notification_claim.assert_not_awaited()

    async def test_temporary_failure_uses_retry_path(self):
        worker, _bot, _user, data, _log = fake_worker_dependencies(
            send_side_effect=discord_error(discord.HTTPException, 503),
        )
        await worker.process_once()
        self.assertEqual(
            data.retry_notification_claim.await_args.args[1],
            'discord_server_error',
        )
        data.fail_notification_claim.assert_not_awaited()

    async def test_database_loss_leaves_claim_for_lease_recovery(self):
        secret = 'postgresql://private-password@database/private'
        worker, _bot, user, data, log = fake_worker_dependencies()
        data.fetch_notification_claim_details.side_effect = RuntimeError(secret)
        await worker.process_once()
        user.send.assert_not_awaited()
        data.mark_notification_claim_delivered.assert_not_awaited()
        data.retry_notification_claim.assert_not_awaited()
        data.fail_notification_claim.assert_not_awaited()
        logged = ' '.join(
            str(value)
            for call in log.warning.call_args_list
            for value in call.args
        )
        self.assertNotIn(secret, logged)

    async def test_exception_body_is_never_logged(self):
        secret = 'never-log-this-discord-response-body'
        worker, _bot, _user, _data, log = fake_worker_dependencies(
            send_side_effect=RuntimeError(secret),
        )
        await worker.process_once()
        logged = ' '.join(
            str(value)
            for method in (log.info, log.warning, log.error)
            for call in method.call_args_list
            for value in call.args
        )
        self.assertNotIn(secret, logged)

    async def test_worker_waits_for_readiness_before_polling(self):
        ready = asyncio.Event()
        worker, bot, _user, data, _log = fake_worker_dependencies()
        bot.wait_until_ready.side_effect = ready.wait
        data.claim_notification_batch.return_value = []

        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0)
        data.claim_notification_batch.assert_not_awaited()
        ready.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        data.claim_notification_batch.assert_awaited_once()
        worker.request_stop()
        await asyncio.wait_for(task, timeout=1)


class _FakeTransaction:
    def __init__(self):
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _exc, _traceback):
        self.rolled_back = exc_type is not None
        return False


class _FailingClaimCursor:
    def __init__(self):
        self.mode = None
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, query, _params):
        if 'SELECT accounts.userid' in query:
            self.mode = 'candidate'
        elif 'AS has_active_claim' in query:
            self.mode = 'active_claim'
        elif 'SELECT notificationid' in query and 'status = \'pending\'' in query:
            self.mode = 'notifications'
        elif "SET\n              status = 'processing'" in query:
            raise RuntimeError('simulated claim update failure')

    async def fetchone(self):
        if self.mode == 'candidate':
            return {'userid': 123}
        if self.mode == 'active_claim':
            return {'has_active_claim': False}
        return None

    async def fetchall(self):
        if self.mode == 'notifications':
            return [{'notificationid': 456}]
        return []


class _FailingClaimConnection:
    def __init__(self):
        self.transaction_state = _FakeTransaction()
        self.cursor_state = _FailingClaimCursor()

    def transaction(self):
        return self.transaction_state

    def cursor(self):
        return self.cursor_state


class _FailingClaimConnector:
    def __init__(self):
        self.connection_state = _FailingClaimConnection()

    @asynccontextmanager
    async def connection(self):
        yield self.connection_state


class DataTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_update_failure_rolls_back_transaction(self):
        connector = _FailingClaimConnector()
        data = GoStudyRewardsData()
        data._conn = connector

        with mock.patch(
            'modules.gostudy_rewards.data.notification_cursor',
            return_value=connector.connection_state.cursor_state,
        ):
            with self.assertRaisesRegex(RuntimeError, 'claim update failure'):
                await data.claim_notification_batch(uuid4(), 'test-worker')

        self.assertTrue(connector.connection_state.transaction_state.rolled_back)

    def test_only_fixed_failure_codes_can_be_persisted(self):
        from modules.gostudy_rewards.data import safe_notification_failure_code

        self.assertEqual(
            safe_notification_failure_code('dm_forbidden'),
            'dm_forbidden',
        )
        with self.assertRaisesRegex(ValueError, 'Unrecognized'):
            safe_notification_failure_code('private exception body')

    def test_retry_sql_has_exact_schedule_and_claim_token_guard(self):
        source = (ROOT / 'src/modules/gostudy_rewards/data.py').read_text(
            encoding='utf-8'
        )
        for interval in (
            "interval '1 minute'",
            "interval '5 minutes'",
            "interval '30 minutes'",
            "interval '2 hours'",
            "interval '12 hours'",
        ):
            self.assertIn(interval, source)
        self.assertGreaterEqual(
            source.count("WHERE status = 'processing' AND claim_token = %s"),
            3,
        )


def load_cog_module():
    fake_meta = ModuleType('meta')
    fake_meta.LionBot = object
    fake_meta.LionCog = commands.Cog
    sys.modules.pop('modules.gostudy_rewards.cog', None)
    with mock.patch.dict(sys.modules, {'meta': fake_meta}):
        return importlib.import_module('modules.gostudy_rewards.cog')


class NotificationCogLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_starts_once_and_unload_drains_cleanly(self):
        cog_module = load_cog_module()
        parser = configparser.ConfigParser()
        parser.read_dict({
            'GO_STUDY': {
                'notifications_enabled': 'true',
                'web_url': 'https://study.example',
            }
        })
        data = SimpleNamespace(init=mock.AsyncMock())
        bot = SimpleNamespace(
            db=SimpleNamespace(load_registry=mock.Mock(return_value=data)),
            config=SimpleNamespace(config=parser),
            shardname='GoStudy_01_00',
        )

        stopped = asyncio.Event()
        fake_worker = SimpleNamespace(
            run=mock.AsyncMock(side_effect=stopped.wait),
            request_stop=mock.Mock(side_effect=stopped.set),
        )
        factory = mock.Mock(return_value=fake_worker)
        with mock.patch.object(cog_module, 'RewardNotificationWorker', factory):
            cog = cog_module.GoStudyRewardsCog(bot)
            await cog.cog_load()
            first_task = cog.notification_task
            await cog.cog_load()
            await asyncio.sleep(0)

            factory.assert_called_once()
            self.assertIs(cog.notification_task, first_task)
            fake_worker.run.assert_awaited_once()
            await cog.cog_unload()

        fake_worker.request_stop.assert_called_once_with()
        self.assertTrue(first_task.done())
        self.assertIsNone(cog.notification_task)

    async def test_disabled_notifications_load_data_without_starting_worker(self):
        cog_module = load_cog_module()
        parser = configparser.ConfigParser()
        data = SimpleNamespace(init=mock.AsyncMock())
        bot = SimpleNamespace(
            db=SimpleNamespace(load_registry=mock.Mock(return_value=data)),
            config=SimpleNamespace(config=parser),
            shardname='GoStudy_01_00',
        )
        factory = mock.Mock()
        with mock.patch.object(cog_module, 'RewardNotificationWorker', factory):
            cog = cog_module.GoStudyRewardsCog(bot)
            await cog.cog_load()

        data.init.assert_awaited_once_with()
        factory.assert_not_called()
        self.assertIsNone(cog.notification_task)


if __name__ == '__main__':
    unittest.main()
