import logging

import discord

from meta import LionBot, LionCog

from .data import GoStudyGuildRegistryData
from .service import GuildRegistryService


logger = logging.getLogger(__name__)


class GoStudyGuildRegistryCog(LionCog):
    """Thin Discord event boundary for the Go Study guild registry."""

    def __init__(self, bot: LionBot):
        self.bot = bot
        self.data = bot.db.load_registry(GoStudyGuildRegistryData())
        self.service = GuildRegistryService(self.data)

    async def cog_load(self):
        await self.data.init()

    async def _sync_event(self, guild: discord.Guild) -> None:
        try:
            await self.service.sync_guild(guild)
        except Exception:
            logger.exception(
                'Go Study guild event sync failed for guild ID %s.',
                guild.id,
            )

    @LionCog.listener('on_ready')
    async def reconcile_connected_guilds(self) -> None:
        summary = await self.service.sync_all_guilds(self.bot.guilds)
        logger.info(
            'Go Study guild startup reconciliation completed: '
            '%s attempted, %s succeeded, %s failed.',
            summary.attempted,
            summary.succeeded,
            summary.failed,
        )

    @LionCog.listener('on_guild_join')
    async def sync_joined_guild(self, guild: discord.Guild) -> None:
        await self._sync_event(guild)

    @LionCog.listener('on_guild_remove')
    async def mark_removed_guild_inactive(self, guild: discord.Guild) -> None:
        try:
            await self.service.mark_guild_inactive(guild)
        except Exception:
            logger.exception(
                'Go Study guild leave sync failed for guild ID %s.',
                guild.id,
            )

    @LionCog.listener('on_guild_update')
    async def sync_updated_guild(
        self,
        before: discord.Guild,
        after: discord.Guild,
    ) -> None:
        await self._sync_event(after)

    @LionCog.listener('on_guild_emojis_update')
    async def sync_updated_emojis(
        self,
        guild: discord.Guild,
        before: tuple[discord.Emoji, ...],
        after: tuple[discord.Emoji, ...],
    ) -> None:
        await self._sync_event(guild)

    @LionCog.listener('on_guild_stickers_update')
    async def sync_updated_stickers(
        self,
        guild: discord.Guild,
        before: tuple[discord.GuildSticker, ...],
        after: tuple[discord.GuildSticker, ...],
    ) -> None:
        await self._sync_event(guild)
