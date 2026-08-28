from typing import Any, Optional
from uuid import UUID

from data import ORDER, Registry, RowModel
from data.columns import Bool, Column, Integer, String, Timestamp
from psycopg import AsyncCursor
from psycopg.rows import dict_row


SAFE_NOTIFICATION_FAILURE_CODES = frozenset({
    'lease_expired',
    'retry_exhausted',
    'dm_forbidden',
    'user_not_found',
    'discord_http_4xx',
    'discord_rate_limited',
    'discord_server_error',
    'discord_temporary',
    'network_error',
})


def notification_cursor(conn):
    """Use psycopg directly so DB errors propagate without unsafe detail logs."""
    return AsyncCursor(conn, row_factory=dict_row)


def safe_notification_failure_code(failure_code: str) -> str:
    if failure_code not in SAFE_NOTIFICATION_FAILURE_CODES:
        raise ValueError('Unrecognized reward notification failure category.')
    return failure_code


class GoStudyRewardsData(Registry, name='gostudy_rewards'):
    class CatalogItem(RowModel):
        _tablename_ = 'gostudy_reward_catalog'

        catalog_itemid = Integer(primary=True)
        item_key = String()
        display_name = String()
        description = String()
        asset_key = String()
        metadata: Column[dict[str, Any]] = Column()
        selection_order = Integer()
        active = Bool()
        created_at = Timestamp()

        @classmethod
        async def fetch_catalog(cls, *, active_only: bool = True):
            """Fetch the stable catalog in its public display order."""
            query = cls.fetch_where(active=True) if active_only else cls.fetch_where()
            return await query.order_by(cls.selection_order, ORDER.ASC).order_by(
                cls.catalog_itemid, ORDER.ASC
            )

    class VerifiedSessionCredit(RowModel):
        _tablename_ = 'gostudy_verified_session_credits'

        source_sessionid = Integer(primary=True)
        userid = Integer()
        source_guildid = Integer()
        verified_seconds = Integer()
        processed_at = Timestamp()

    class RewardAccount(RowModel):
        _tablename_ = 'gostudy_reward_accounts'

        userid = Integer(primary=True)
        verified_seconds = Integer()
        updated_at = Timestamp()

    class HourReward(RowModel):
        _tablename_ = 'gostudy_hour_rewards'

        rewardid = Integer(primary=True)
        userid = Integer()
        milestone_hour = Integer()
        source_sessionid = Integer()
        verified_seconds_at_award = Integer()
        earned_at = Timestamp()

        @classmethod
        async def fetch_for_user(cls, userid: int, *, after_rewardid: int = 0, limit: int = 100):
            """Fetch an ordered page suitable for a future web/API consumer."""
            limit = max(1, min(limit, 1000))
            return await cls.fetch_where(
                cls.rewardid > after_rewardid,
                userid=userid
            ).order_by(cls.rewardid, ORDER.ASC).limit(limit)

    class InventoryItem(RowModel):
        _tablename_ = 'gostudy_user_inventory'

        hour_rewardid = Integer(primary=True)
        catalog_itemid = Integer()
        granted_at = Timestamp()

    class RewardNotification(RowModel):
        _tablename_ = 'gostudy_reward_notifications'

        notificationid = Integer(primary=True)
        hour_rewardid = Integer()
        userid = Integer()
        status = String()
        attempt_count = Integer()
        next_attempt_at = Timestamp()
        last_attempt_at = Timestamp()
        claim_token: Column[UUID] = Column()
        claimed_by = String()
        lease_expires_at = Timestamp()
        delivered_at = Timestamp()
        failed_at = Timestamp()
        last_failure_code = String()
        created_at = Timestamp()
        updated_at = Timestamp()

    async def process_voice_session(self, sessionid: int) -> int:
        """Idempotently process one completed session and return rewards created."""
        async with self._conn.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT gostudy_process_voice_session(%s) AS rewards_created",
                    (sessionid,)
                )
                row = await cursor.fetchone()
                return row['rewards_created']

    async def grant_hour_reward(self, rewardid: int) -> int:
        """Explicitly grant or replay one hour reward without duplicating it."""
        async with self._conn.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT gostudy_grant_hour_reward(%s) AS items_granted",
                    (rewardid,)
                )
                row = await cursor.fetchone()
                return row['items_granted']

    async def fetch_inventory_for_user(
        self,
        userid: int,
        *,
        before_hour_rewardid: Optional[int] = None,
        limit: int = 100
    ):
        """Fetch a web/API-ready page of globally owned inventory items."""
        limit = max(1, min(limit, 1000))
        query = """
            SELECT
              inventory.hour_rewardid AS inventory_item_id,
              rewards.userid,
              rewards.milestone_hour,
              rewards.earned_at,
              inventory.granted_at,
              catalog.catalog_itemid,
              catalog.item_key,
              catalog.display_name,
              catalog.description,
              catalog.asset_key,
              catalog.metadata,
              catalog.active
            FROM gostudy_user_inventory AS inventory
            JOIN gostudy_hour_rewards AS rewards
              ON rewards.rewardid = inventory.hour_rewardid
            JOIN gostudy_reward_catalog AS catalog
              ON catalog.catalog_itemid = inventory.catalog_itemid
            WHERE
              rewards.userid = %s
              AND (%s::BIGINT IS NULL OR inventory.hour_rewardid < %s)
            ORDER BY rewards.earned_at DESC, inventory.hour_rewardid DESC
            LIMIT %s
        """
        async with self._conn.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    query,
                    (userid, before_hour_rewardid, before_hour_rewardid, limit)
                )
                return await cursor.fetchall()

    async def _recover_expired_claims(
        self,
        conn,
        *,
        max_attempts: int,
        recovery_limit: int,
    ) -> tuple[int, int]:
        """Recover a bounded set of expired claims inside an existing transaction."""
        terminal_query = """
            WITH expired AS (
              SELECT notificationid
              FROM gostudy_reward_notifications
              WHERE
                status = 'processing'
                AND lease_expires_at <= now()
                AND attempt_count >= %s
              ORDER BY lease_expires_at, notificationid
              FOR UPDATE SKIP LOCKED
              LIMIT %s
            )
            UPDATE gostudy_reward_notifications AS notifications
            SET
              status = 'failed',
              next_attempt_at = NULL,
              claim_token = NULL,
              claimed_by = NULL,
              lease_expires_at = NULL,
              failed_at = now(),
              last_failure_code = 'retry_exhausted',
              updated_at = now()
            FROM expired
            WHERE notifications.notificationid = expired.notificationid
        """
        pending_query = """
            WITH expired AS (
              SELECT notificationid
              FROM gostudy_reward_notifications
              WHERE
                status = 'processing'
                AND lease_expires_at <= now()
                AND attempt_count < %s
              ORDER BY lease_expires_at, notificationid
              FOR UPDATE SKIP LOCKED
              LIMIT %s
            )
            UPDATE gostudy_reward_notifications AS notifications
            SET
              status = 'pending',
              next_attempt_at = now(),
              claim_token = NULL,
              claimed_by = NULL,
              lease_expires_at = NULL,
              last_failure_code = 'lease_expired',
              updated_at = now()
            FROM expired
            WHERE notifications.notificationid = expired.notificationid
        """
        async with notification_cursor(conn) as cursor:
            await cursor.execute(terminal_query, (max_attempts, recovery_limit))
            terminal_count = cursor.rowcount
            await cursor.execute(pending_query, (max_attempts, recovery_limit))
            pending_count = cursor.rowcount
        return pending_count, terminal_count

    async def recover_expired_claims(
        self,
        *,
        max_attempts: int = 6,
        recovery_limit: int = 100,
    ) -> tuple[int, int]:
        """Recover expired leases without waiting for another claim to be made."""
        max_attempts = max(1, min(max_attempts, 6))
        recovery_limit = max(1, min(recovery_limit, 1000))
        async with self._conn.connection() as conn:
            async with conn.transaction():
                return await self._recover_expired_claims(
                    conn,
                    max_attempts=max_attempts,
                    recovery_limit=recovery_limit,
                )

    async def claim_notification_batch(
        self,
        claim_token: UUID,
        claimed_by: str,
        *,
        batch_size: int = 10,
        lease_seconds: int = 300,
        max_attempts: int = 6,
    ):
        """Claim one user's due notifications in a short transaction."""
        batch_size = max(1, min(batch_size, 10))
        lease_seconds = max(1, min(lease_seconds, 3600))
        max_attempts = max(1, min(max_attempts, 6))
        claimed_by = claimed_by[:128]

        candidate_query = """
            SELECT accounts.userid
            FROM gostudy_reward_accounts AS accounts
            WHERE
              EXISTS (
                SELECT 1
                FROM gostudy_reward_notifications AS due
                WHERE
                  due.userid = accounts.userid
                  AND due.status = 'pending'
                  AND due.next_attempt_at <= now()
                  AND due.attempt_count < %s
              )
              AND NOT EXISTS (
                SELECT 1
                FROM gostudy_reward_notifications AS active_claim
                WHERE
                  active_claim.userid = accounts.userid
                  AND active_claim.status = 'processing'
                  AND active_claim.lease_expires_at > now()
              )
            ORDER BY (
              SELECT min(due.next_attempt_at)
              FROM gostudy_reward_notifications AS due
              WHERE
                due.userid = accounts.userid
                AND due.status = 'pending'
                AND due.next_attempt_at <= now()
                AND due.attempt_count < %s
            ), accounts.userid
            FOR UPDATE OF accounts SKIP LOCKED
            LIMIT 1
        """
        notification_query = """
            SELECT notificationid
            FROM gostudy_reward_notifications
            WHERE
              userid = %s
              AND status = 'pending'
              AND next_attempt_at <= now()
              AND attempt_count < %s
            ORDER BY next_attempt_at, notificationid
            FOR UPDATE SKIP LOCKED
            LIMIT %s
        """
        active_claim_query = """
            SELECT EXISTS (
              SELECT 1
              FROM gostudy_reward_notifications
              WHERE
                userid = %s
                AND status = 'processing'
                AND lease_expires_at > now()
            ) AS has_active_claim
        """
        claim_query = """
            UPDATE gostudy_reward_notifications
            SET
              status = 'processing',
              attempt_count = attempt_count + 1,
              next_attempt_at = NULL,
              last_attempt_at = now(),
              claim_token = %s,
              claimed_by = %s,
              lease_expires_at = now() + make_interval(secs => %s),
              updated_at = now()
            WHERE notificationid = ANY(%s::BIGINT[])
            RETURNING
              notificationid,
              hour_rewardid,
              userid,
              attempt_count,
              claim_token,
              lease_expires_at
        """

        async with self._conn.connection() as conn:
            async with conn.transaction():
                await self._recover_expired_claims(
                    conn,
                    max_attempts=max_attempts,
                    recovery_limit=100,
                )
                async with notification_cursor(conn) as cursor:
                    await cursor.execute(
                        candidate_query,
                        (max_attempts, max_attempts),
                    )
                    candidate = await cursor.fetchone()
                    if candidate is None:
                        return []

                    # The candidate predicate and account-row lock occur in
                    # one statement snapshot. If another worker commits just
                    # before this transaction acquires that row lock, use a
                    # fresh statement snapshot while still holding the mutex.
                    await cursor.execute(
                        active_claim_query,
                        (candidate['userid'],),
                    )
                    active_claim = await cursor.fetchone()
                    if active_claim['has_active_claim']:
                        return []

                    await cursor.execute(
                        notification_query,
                        (candidate['userid'], max_attempts, batch_size),
                    )
                    notification_ids = [
                        row['notificationid'] for row in await cursor.fetchall()
                    ]
                    if not notification_ids:
                        return []

                    await cursor.execute(
                        claim_query,
                        (claim_token, claimed_by, lease_seconds, notification_ids),
                    )
                    return await cursor.fetchall()

    async def fetch_notification_claim_details(self, claim_token: UUID):
        """Fetch the granted catalog items for the current processing claim."""
        query = """
            SELECT
              notifications.notificationid,
              notifications.hour_rewardid,
              notifications.userid,
              notifications.attempt_count,
              rewards.milestone_hour,
              inventory.catalog_itemid,
              catalog.item_key,
              catalog.display_name
            FROM gostudy_reward_notifications AS notifications
            JOIN gostudy_hour_rewards AS rewards
              ON rewards.rewardid = notifications.hour_rewardid
            LEFT JOIN gostudy_user_inventory AS inventory
              ON inventory.hour_rewardid = rewards.rewardid
            LEFT JOIN gostudy_reward_catalog AS catalog
              ON catalog.catalog_itemid = inventory.catalog_itemid
            WHERE
              notifications.status = 'processing'
              AND notifications.claim_token = %s
            ORDER BY notifications.notificationid
        """
        async with self._conn.connection() as conn:
            async with notification_cursor(conn) as cursor:
                await cursor.execute(query, (claim_token,))
                return await cursor.fetchall()

    async def mark_notification_claim_delivered(self, claim_token: UUID) -> int:
        """Acknowledge only rows still owned by the current claim token."""
        query = """
            UPDATE gostudy_reward_notifications
            SET
              status = 'delivered',
              next_attempt_at = NULL,
              claim_token = NULL,
              claimed_by = NULL,
              lease_expires_at = NULL,
              delivered_at = now(),
              last_failure_code = NULL,
              updated_at = now()
            WHERE status = 'processing' AND claim_token = %s
        """
        async with self._conn.connection() as conn:
            async with notification_cursor(conn) as cursor:
                await cursor.execute(query, (claim_token,))
                return cursor.rowcount

    async def retry_notification_claim(
        self,
        claim_token: UUID,
        failure_code: str,
        *,
        max_attempts: int = 6,
    ) -> int:
        """Schedule fixed per-attempt backoff or terminally exhaust the claim."""
        max_attempts = max(1, min(max_attempts, 6))
        failure_code = safe_notification_failure_code(failure_code)
        query = """
            UPDATE gostudy_reward_notifications
            SET
              status = CASE
                WHEN attempt_count >= %s THEN 'failed'
                ELSE 'pending'
              END,
              next_attempt_at = CASE
                WHEN attempt_count >= %s THEN NULL
                ELSE CASE attempt_count
                  WHEN 1 THEN now() + interval '1 minute'
                  WHEN 2 THEN now() + interval '5 minutes'
                  WHEN 3 THEN now() + interval '30 minutes'
                  WHEN 4 THEN now() + interval '2 hours'
                  WHEN 5 THEN now() + interval '12 hours'
                  ELSE now() + interval '12 hours'
                END
              END,
              claim_token = NULL,
              claimed_by = NULL,
              lease_expires_at = NULL,
              failed_at = CASE
                WHEN attempt_count >= %s THEN now()
                ELSE NULL
              END,
              last_failure_code = CASE
                WHEN attempt_count >= %s THEN 'retry_exhausted'
                ELSE %s
              END,
              updated_at = now()
            WHERE status = 'processing' AND claim_token = %s
        """
        async with self._conn.connection() as conn:
            async with notification_cursor(conn) as cursor:
                await cursor.execute(
                    query,
                    (
                        max_attempts,
                        max_attempts,
                        max_attempts,
                        max_attempts,
                        failure_code,
                        claim_token,
                    ),
                )
                return cursor.rowcount

    async def fail_notification_claim(
        self,
        claim_token: UUID,
        failure_code: str,
    ) -> int:
        """Mark only the current claim as terminally failed."""
        failure_code = safe_notification_failure_code(failure_code)
        query = """
            UPDATE gostudy_reward_notifications
            SET
              status = 'failed',
              next_attempt_at = NULL,
              claim_token = NULL,
              claimed_by = NULL,
              lease_expires_at = NULL,
              failed_at = now(),
              last_failure_code = %s,
              updated_at = now()
            WHERE status = 'processing' AND claim_token = %s
        """
        async with self._conn.connection() as conn:
            async with notification_cursor(conn) as cursor:
                await cursor.execute(
                    query,
                    (failure_code, claim_token),
                )
                return cursor.rowcount
