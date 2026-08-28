from typing import Any, Optional

from data import ORDER, Registry, RowModel
from data.columns import Bool, Column, Integer, String, Timestamp


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
