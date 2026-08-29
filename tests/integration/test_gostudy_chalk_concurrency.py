"""Disposable PostgreSQL integration tests for Go Study Chalk schema v18.

The tests commit immutable ledger rows and are deliberately double-gated:

GOSTUDY_CHALK_TEST_DATABASE_URL=... \
GOSTUDY_CHALK_TEST_ALLOW_WRITES=1 \
  python -m unittest tests/integration/test_gostudy_chalk_concurrency.py

Never point these variables at the real lion_data database.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import random
import sys
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import tuple_row


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from data.connector import Connector  # noqa: E402
from modules.gostudy_chalk.data import (  # noqa: E402
    ChalkTransactionResult,
    GoStudyChalkData,
)


TEST_DATABASE_URL = os.environ.get('GOSTUDY_CHALK_TEST_DATABASE_URL', '')
WRITES_ALLOWED = os.environ.get('GOSTUDY_CHALK_TEST_ALLOW_WRITES') == '1'
TEST_APP_ROLE = os.environ.get('GOSTUDY_CHALK_TEST_APP_ROLE', '')
MIGRATION_ADMIN_URL = os.environ.get(
    'GOSTUDY_CHALK_MIGRATION_TEST_ADMIN_URL',
    '',
)
MIGRATION_WRITES_ALLOWED = (
    os.environ.get('GOSTUDY_CHALK_MIGRATION_TEST_ALLOW_WRITES') == '1'
)


@unittest.skipUnless(
    TEST_DATABASE_URL and WRITES_ALLOWED,
    'requires an explicitly authorized disposable PostgreSQL schema v18 database',
)
class ChalkConcurrencyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connector = Connector(TEST_DATABASE_URL)
        self.pool_context = self.connector.open()
        await self.pool_context.__aenter__()

        async with self.connector.connection() as conn:
            async with psycopg.AsyncCursor(conn, row_factory=tuple_row) as cursor:
                await cursor.execute(
                    'SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1'
                )
                row = await cursor.fetchone()
        if row is None or row[0] != 18:
            await self.pool_context.__aexit__(None, None, None)
            raise unittest.SkipTest('disposable database is not schema version 18')

        self.data = GoStudyChalkData()
        self.data.bind(self.connector)
        await self.data.init()
        self.actor_userid = 918999999999999999
        self.userid = 910000000000000000 + random.SystemRandom().randrange(10**14)
        self.key_prefix = f'chalk-pg:{uuid4()}'

    async def asyncTearDown(self):
        await self.pool_context.__aexit__(None, None, None)

    async def grant(self, amount: int, suffix: str, *, userid: int | None = None):
        return await self.data.apply_transaction(
            userid or self.userid,
            amount,
            'admin_grant',
            f'{self.key_prefix}:{suffix}',
            actor_userid=self.actor_userid,
            reason=f'Integration grant {suffix}',
        )

    async def test_missing_account_reads_zero_without_creating_row(self):
        account = await self.data.fetch_account(self.userid)
        self.assertEqual(account.balance, 0)
        self.assertEqual(account.lifetime_credited, 0)
        self.assertEqual(account.lifetime_debited, 0)
        self.assertIsNone(account.created_at)

        async with self.connector.connection() as conn:
            async with psycopg.AsyncCursor(conn, row_factory=tuple_row) as cursor:
                await cursor.execute(
                    'SELECT count(*) FROM public.gostudy_chalk_accounts WHERE userid = %s',
                    (self.userid,),
                )
                row = await cursor.fetchone()
        self.assertEqual(row[0], 0)

    async def test_exact_replay_preserves_historical_and_current_balances(self):
        first = await self.grant(5, 'historical-first')
        await self.grant(2, 'historical-later')
        replay = await self.data.apply_transaction(
            self.userid,
            5,
            'admin_grant',
            f'{self.key_prefix}:historical-first',
            actor_userid=self.actor_userid,
            reason='Integration grant historical-first',
        )

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.transaction.transactionid, replay.transaction.transactionid)
        self.assertEqual(replay.transaction.balance_after, 5)
        self.assertEqual(replay.account.balance, 7)

    async def test_simultaneous_exact_replay_applies_once(self):
        async def apply():
            return await self.data.apply_transaction(
                self.userid,
                10,
                'admin_grant',
                f'{self.key_prefix}:same-key',
                actor_userid=self.actor_userid,
                reason='Concurrent exact replay',
            )

        results = await asyncio.gather(apply(), apply())
        self.assertEqual([result.replayed for result in results].count(False), 1)
        self.assertEqual([result.replayed for result in results].count(True), 1)
        self.assertEqual(await self.data.fetch_balance(self.userid), 10)
        history = await self.data.fetch_transactions(self.userid)
        self.assertEqual(len(history), 1)

    async def test_changed_economic_payload_fields_conflict(self):
        original = await self.data.apply_transaction(
            self.userid,
            10,
            'admin_grant',
            f'{self.key_prefix}:payload',
            actor_userid=self.actor_userid,
            reference_type='admin_request',
            reference_id='request-one',
            reason='Original payload',
        )
        other_userid = self.userid + 1
        cases = (
            {'userid': other_userid},
            {'amount': 11},
            {'transaction_type': 'system_adjustment'},
            {'actor_userid': self.actor_userid - 1},
            {'reference_type': 'system_request'},
            {'reference_id': 'request-two'},
            {'reversal_of_transactionid': original.transaction.transactionid},
            {'reason': 'Changed payload'},
        )

        base = {
            'userid': self.userid,
            'amount': 10,
            'transaction_type': 'admin_grant',
            'idempotency_key': f'{self.key_prefix}:payload',
            'actor_userid': self.actor_userid,
            'reference_type': 'admin_request',
            'reference_id': 'request-one',
            'reversal_of_transactionid': None,
            'reason': 'Original payload',
        }
        for changed in cases:
            request = base | changed
            with self.subTest(changed=changed):
                with self.assertRaises(psycopg.Error) as caught:
                    await self.data.apply_transaction(
                        request['userid'],
                        request['amount'],
                        request['transaction_type'],
                        request['idempotency_key'],
                        actor_userid=request['actor_userid'],
                        reference_type=request['reference_type'],
                        reference_id=request['reference_id'],
                        reversal_of_transactionid=request['reversal_of_transactionid'],
                        reason=request['reason'],
                    )
                self.assertEqual(caught.exception.sqlstate, '22000')

        self.assertEqual(await self.data.fetch_balance(self.userid), 10)

    async def test_two_concurrent_grants_both_count(self):
        first, second = await asyncio.gather(
            self.grant(7, 'grant-a'),
            self.grant(9, 'grant-b'),
        )
        self.assertIsInstance(first, ChalkTransactionResult)
        self.assertIsInstance(second, ChalkTransactionResult)
        account = await self.data.fetch_account(self.userid)
        self.assertEqual(account.balance, 16)
        self.assertEqual(account.lifetime_credited, 16)
        self.assertEqual(account.lifetime_debited, 0)

    async def test_two_concurrent_purchases_cannot_overspend(self):
        await self.grant(10, 'purchase-funding')

        async def purchase(suffix: str):
            return await self.data.apply_transaction(
                self.userid,
                -7,
                'shop_purchase',
                f'{self.key_prefix}:{suffix}',
                reference_type='board_purchase',
                reference_id=suffix,
            )

        outcomes = await asyncio.gather(
            purchase('purchase-a'),
            purchase('purchase-b'),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if isinstance(item, ChalkTransactionResult)]
        failures = [item for item in outcomes if isinstance(item, BaseException)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], psycopg.Error)
        self.assertEqual(failures[0].sqlstate, '23514')

        account = await self.data.fetch_account(self.userid)
        self.assertEqual(account.balance, 3)
        self.assertEqual(account.lifetime_credited, 10)
        self.assertEqual(account.lifetime_debited, 7)

    async def test_two_concurrent_refunds_cannot_over_refund(self):
        await self.grant(20, 'refund-funding')
        purchase = await self.data.apply_transaction(
            self.userid,
            -10,
            'shop_purchase',
            f'{self.key_prefix}:refund-purchase',
            reference_type='board_purchase',
            reference_id='refund-purchase',
        )

        async def refund(suffix: str):
            return await self.data.apply_transaction(
                self.userid,
                7,
                'refund',
                f'{self.key_prefix}:{suffix}',
                reversal_of_transactionid=purchase.transaction.transactionid,
            )

        outcomes = await asyncio.gather(
            refund('refund-a'),
            refund('refund-b'),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if isinstance(item, ChalkTransactionResult)]
        failures = [item for item in outcomes if isinstance(item, BaseException)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], psycopg.Error)
        self.assertEqual(failures[0].sqlstate, '22023')

        account = await self.data.fetch_account(self.userid)
        self.assertEqual(account.balance, 17)
        self.assertEqual(account.lifetime_credited, 27)
        self.assertEqual(account.lifetime_debited, 10)

        async with self.connector.connection() as conn:
            async with psycopg.AsyncCursor(conn, row_factory=tuple_row) as cursor:
                await cursor.execute(
                    """
                    SELECT COALESCE(sum(amount::NUMERIC), 0::NUMERIC)
                    FROM public.gostudy_chalk_transactions
                    WHERE transaction_type = 'refund'
                      AND reversal_of_transactionid = %s
                    """,
                    (purchase.transaction.transactionid,),
                )
                row = await cursor.fetchone()
        self.assertEqual(row[0], 7)

    @unittest.skipUnless(
        TEST_APP_ROLE,
        'set GOSTUDY_CHALK_TEST_APP_ROLE to verify an available application role',
    )
    async def test_application_role_has_no_direct_mutation_privileges(self):
        async with self.connector.connection() as conn:
            async with psycopg.AsyncCursor(conn, row_factory=tuple_row) as cursor:
                for table in (
                    'public.gostudy_chalk_accounts',
                    'public.gostudy_chalk_transactions',
                ):
                    for privilege in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'):
                        await cursor.execute(
                            'SELECT has_table_privilege(%s, %s, %s)',
                            (TEST_APP_ROLE, table, privilege),
                        )
                        row = await cursor.fetchone()
                        self.assertFalse(
                            row[0],
                            f'{TEST_APP_ROLE} unexpectedly has {privilege} on {table}',
                        )


@unittest.skipUnless(
    MIGRATION_ADMIN_URL and MIGRATION_WRITES_ALLOWED,
    'requires an explicitly authorized disposable PostgreSQL admin target',
)
class ChalkMigrationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Create and remove isolated databases to exercise migration boundaries."""

    @classmethod
    def setUpClass(cls):
        cls.schema_v18 = (ROOT / 'data/schema.sql').read_text(encoding='utf-8')
        cls.migration = (
            ROOT / 'data/migration/v17-v18/migration.sql'
        ).read_text(encoding='utf-8')

        chalk_start = cls.schema_v18.index('-- Go Study Chalk currency {{{')
        chalk_end = cls.schema_v18.index('-- }}}', chalk_start) + len('-- }}}')
        without_chalk = (
            cls.schema_v18[:chalk_start] + cls.schema_v18[chalk_end:]
        )
        cls.schema_v17 = without_chalk.replace(
            "INSERT INTO VersionHistory (version, author) VALUES (18, 'Initial Creation');",
            "INSERT INTO VersionHistory (version, author) VALUES (17, 'Initial Creation');",
            1,
        )

    async def asyncSetUp(self):
        self.database_name = f'gostudy_chalk_{uuid4().hex}'
        self.admin = await psycopg.AsyncConnection.connect(
            MIGRATION_ADMIN_URL,
            autocommit=True,
        )
        async with self.admin.cursor() as cursor:
            await cursor.execute(
                sql.SQL(
                    "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'"
                ).format(
                    sql.Identifier(self.database_name)
                )
            )
        self.database_url = make_conninfo(
            MIGRATION_ADMIN_URL,
            dbname=self.database_name,
        )

    async def asyncTearDown(self):
        if not self.admin.closed:
            async with self.admin.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = %s AND pid <> pg_backend_pid()
                    """,
                    (self.database_name,),
                )
                await cursor.execute(
                    sql.SQL('DROP DATABASE {}').format(
                        sql.Identifier(self.database_name)
                    )
                )
            await self.admin.close()

    async def connect_database(self):
        return await psycopg.AsyncConnection.connect(
            self.database_url,
            autocommit=True,
        )

    async def test_version_17_migrates_and_reapplication_rolls_back(self):
        conn = await self.connect_database()
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(self.schema_v17)
                await cursor.execute(self.migration)
                await cursor.execute(
                    'SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1'
                )
                self.assertEqual((await cursor.fetchone())[0], 18)
                await cursor.execute(
                    'SELECT count(*) FROM public.gostudy_chalk_accounts'
                )
                self.assertEqual((await cursor.fetchone())[0], 0)
                await cursor.execute('SELECT count(*) FROM VersionHistory')
                history_count = (await cursor.fetchone())[0]

                with self.assertRaises(psycopg.Error):
                    await cursor.execute(self.migration)
                await conn.rollback()

                await cursor.execute(
                    'SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1'
                )
                self.assertEqual((await cursor.fetchone())[0], 18)
                await cursor.execute('SELECT count(*) FROM VersionHistory')
                self.assertEqual((await cursor.fetchone())[0], history_count)
        finally:
            await conn.close()

    async def test_version_16_rejects_without_objects_or_history_change(self):
        conn = await self.connect_database()
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(self.schema_v17)
                await cursor.execute(
                    'UPDATE VersionHistory SET version = 16'
                )
                await cursor.execute(
                    'SELECT version, time, author FROM VersionHistory ORDER BY time'
                )
                history_before = await cursor.fetchall()

                with self.assertRaises(psycopg.Error):
                    await cursor.execute(self.migration)
                await conn.rollback()

                await cursor.execute(
                    'SELECT version, time, author FROM VersionHistory ORDER BY time'
                )
                self.assertEqual(await cursor.fetchall(), history_before)
                await cursor.execute(
                    "SELECT to_regclass('public.gostudy_chalk_accounts')"
                )
                self.assertIsNone((await cursor.fetchone())[0])
        finally:
            await conn.close()

    async def test_fresh_v18_schema_runs_chalk_and_reward_regressions(self):
        conn = await self.connect_database()
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(self.schema_v18)
                await cursor.execute(
                    'SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1'
                )
                self.assertEqual((await cursor.fetchone())[0], 18)

                for relative_path in (
                    'tests/integration/gostudy_chalk.sql',
                    'tests/integration/gostudy_rewards.sql',
                    'tests/integration/gostudy_inventory.sql',
                    'tests/integration/gostudy_reward_notifications.sql',
                ):
                    source = (ROOT / relative_path).read_text(encoding='utf-8')
                    await cursor.execute(source)
        finally:
            await conn.close()


if __name__ == '__main__':
    unittest.main()
