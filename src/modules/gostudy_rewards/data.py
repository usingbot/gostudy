from data import ORDER, Registry, RowModel
from data.columns import Integer, Timestamp


class GoStudyRewardsData(Registry, name='gostudy_rewards'):
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
