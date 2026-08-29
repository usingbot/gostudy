"""PostgreSQL concurrency tests for the notification outbox on schema v20.

These tests are deliberately double-gated because they commit short-lived
fixtures and require schema v20. Point them only at a disposable database:

GOSTUDY_NOTIFICATION_TEST_DATABASE_URL=... \
GOSTUDY_NOTIFICATION_TEST_ALLOW_WRITES=1 \
  python -m unittest tests/integration/test_gostudy_reward_notification_claims.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import random
import sys
import unittest
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from data.connector import Connector
from modules.gostudy_rewards.data import GoStudyRewardsData


TEST_DATABASE_URL = os.environ.get('GOSTUDY_NOTIFICATION_TEST_DATABASE_URL', '')
WRITES_ALLOWED = os.environ.get('GOSTUDY_NOTIFICATION_TEST_ALLOW_WRITES') == '1'


@unittest.skipUnless(
    TEST_DATABASE_URL and WRITES_ALLOWED,
    'requires an explicitly authorized disposable PostgreSQL schema v20 database',
)
class RewardNotificationClaimIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connector = Connector(TEST_DATABASE_URL)
        self.pool_context = self.connector.open()
        await self.pool_context.__aenter__()

        async with self.connector.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    'SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1'
                )
                row = await cursor.fetchone()
        if row is None or row['version'] != 20:
            await self.pool_context.__aexit__(None, None, None)
            raise unittest.SkipTest('disposable database is not schema version 20')

        self.userid = -random.randint(1_700_000_000, 1_799_999_999)
        self.source_ids = [
            -random.randint(1_700_000_000, 1_799_999_999)
            for _ in range(12)
        ]
        self.data = GoStudyRewardsData()
        self.data.bind(self.connector)
        await self.data.init()

    async def asyncTearDown(self):
        async with self.connector.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM gostudy_reward_notifications WHERE userid = %s",
                    (self.userid,),
                )
                await cursor.execute(
                    """DELETE FROM gostudy_user_inventory
                    WHERE hour_rewardid IN (
                      SELECT rewardid FROM gostudy_hour_rewards WHERE userid = %s
                    )
                    """,
                    (self.userid,),
                )
                await cursor.execute(
                    "DELETE FROM gostudy_hour_rewards WHERE userid = %s",
                    (self.userid,),
                )
                await cursor.execute(
                    "DELETE FROM gostudy_verified_session_credits WHERE userid = %s",
                    (self.userid,),
                )
                await cursor.execute(
                    "DELETE FROM gostudy_reward_accounts WHERE userid = %s",
                    (self.userid,),
                )
        await self.pool_context.__aexit__(None, None, None)

    async def seed_rewards(self, count: int, *, explicit_bigint: bool = False):
        async with self.connector.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO gostudy_reward_accounts (userid, verified_seconds)
                    VALUES (%s, %s)
                    """,
                    (self.userid, count * 3600),
                )
                for index in range(count):
                    await cursor.execute(
                        """
                        INSERT INTO gostudy_verified_session_credits (
                          source_sessionid, userid, source_guildid, verified_seconds
                        ) VALUES (%s, %s, %s, 3600)
                        """,
                        (self.source_ids[index], self.userid, self.userid),
                    )
                    if explicit_bigint and index == 0:
                        await cursor.execute(
                            """
                            INSERT INTO gostudy_hour_rewards (
                              rewardid,
                              userid,
                              milestone_hour,
                              source_sessionid,
                              verified_seconds_at_award
                            ) VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                9_007_199_254_740_993,
                                self.userid,
                                index + 1,
                                self.source_ids[index],
                                (index + 1) * 3600,
                            ),
                        )
                    else:
                        await cursor.execute(
                            """
                            INSERT INTO gostudy_hour_rewards (
                              userid,
                              milestone_hour,
                              source_sessionid,
                              verified_seconds_at_award
                            ) VALUES (%s, %s, %s, %s)
                            """,
                            (
                                self.userid,
                                index + 1,
                                self.source_ids[index],
                                (index + 1) * 3600,
                            ),
                        )

    async def fetch_outbox(self):
        async with self.connector.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT *
                    FROM gostudy_reward_notifications
                    WHERE userid = %s
                    ORDER BY notificationid
                    """,
                    (self.userid,),
                )
                return await cursor.fetchall()

    async def test_two_workers_claim_once_and_preserve_bigint(self):
        await self.seed_rewards(1, explicit_bigint=True)
        first_token = uuid4()
        second_token = uuid4()
        first, second = await asyncio.gather(
            self.data.claim_notification_batch(first_token, 'test-worker-a'),
            self.data.claim_notification_batch(second_token, 'test-worker-b'),
        )

        claimed_batches = [batch for batch in (first, second) if batch]
        self.assertEqual(len(claimed_batches), 1)
        self.assertEqual(len(claimed_batches[0]), 1)
        self.assertEqual(
            claimed_batches[0][0]['hour_rewardid'],
            9_007_199_254_740_993,
        )

    async def test_one_user_cannot_have_parallel_active_batches(self):
        await self.seed_rewards(11)
        first, second = await asyncio.gather(
            self.data.claim_notification_batch(uuid4(), 'test-worker-a'),
            self.data.claim_notification_batch(uuid4(), 'test-worker-b'),
        )
        self.assertEqual(sum(bool(batch) for batch in (first, second)), 1)
        self.assertEqual(sum(len(batch) for batch in (first, second)), 10)

    async def test_lease_recovery_and_stale_acknowledgement(self):
        await self.seed_rewards(1)
        stale_token = uuid4()
        first_claim = await self.data.claim_notification_batch(
            stale_token,
            'test-worker-a',
        )
        self.assertEqual(len(first_claim), 1)

        async with self.connector.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE gostudy_reward_notifications
                    SET lease_expires_at = now() - interval '1 second'
                    WHERE claim_token = %s
                    """,
                    (stale_token,),
                )

        current_token = uuid4()
        second_claim = await self.data.claim_notification_batch(
            current_token,
            'test-worker-b',
        )
        self.assertEqual(len(second_claim), 1)
        self.assertEqual(
            await self.data.mark_notification_claim_delivered(stale_token),
            0,
        )
        self.assertEqual(
            await self.data.mark_notification_claim_delivered(current_token),
            1,
        )

    async def test_nonexpired_lease_is_not_stolen(self):
        await self.seed_rewards(2)
        token = uuid4()
        first = await self.data.claim_notification_batch(token, 'test-worker-a')
        second = await self.data.claim_notification_batch(uuid4(), 'test-worker-b')
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])
        self.assertEqual(
            await self.data.mark_notification_claim_delivered(token),
            2,
        )

    async def test_exact_backoff_and_attempt_six_exhaustion(self):
        await self.seed_rewards(6)
        async with self.connector.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    WITH ordered AS (
                      SELECT notificationid,
                             row_number() OVER (ORDER BY notificationid) AS position
                      FROM gostudy_reward_notifications
                      WHERE userid = %s
                    )
                    UPDATE gostudy_reward_notifications AS notifications
                    SET attempt_count = ordered.position - 1
                    FROM ordered
                    WHERE notifications.notificationid = ordered.notificationid
                    """,
                    (self.userid,),
                )

        token = uuid4()
        claimed = await self.data.claim_notification_batch(token, 'test-worker')
        self.assertEqual(
            sorted(row['attempt_count'] for row in claimed),
            [1, 2, 3, 4, 5, 6],
        )
        self.assertEqual(
            await self.data.retry_notification_claim(token, 'discord_temporary'),
            6,
        )

        rows = await self.fetch_outbox()
        expected_delays = {
            1: 60,
            2: 5 * 60,
            3: 30 * 60,
            4: 2 * 60 * 60,
            5: 12 * 60 * 60,
        }
        for attempt_count, expected_seconds in expected_delays.items():
            pending = next(
                row for row in rows if row['attempt_count'] == attempt_count
            )
            self.assertEqual(pending['status'], 'pending')
            self.assertEqual(
                (pending['next_attempt_at'] - pending['updated_at']).total_seconds(),
                expected_seconds,
            )

        exhausted = next(row for row in rows if row['attempt_count'] == 6)
        self.assertEqual(exhausted['status'], 'failed')
        self.assertEqual(exhausted['last_failure_code'], 'retry_exhausted')


if __name__ == '__main__':
    unittest.main()
