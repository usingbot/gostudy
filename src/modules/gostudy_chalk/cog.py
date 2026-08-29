from meta import LionBot, LionCog

from .data import GoStudyChalkData


class GoStudyChalkCog(LionCog):
    """Bind the command-free Go Study Chalk data layer."""

    def __init__(self, bot: LionBot):
        self.bot = bot
        self.data = bot.db.load_registry(GoStudyChalkData())

    async def cog_load(self):
        await self.data.init()
