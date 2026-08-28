from meta import LionBot, LionCog

from .data import GoStudyRewardsData


class GoStudyRewardsCog(LionCog):
    """Loads the independent Go Study verified-hour reward data layer."""

    def __init__(self, bot: LionBot):
        self.bot = bot
        self.data = bot.db.load_registry(GoStudyRewardsData())

    async def cog_load(self):
        await self.data.init()
