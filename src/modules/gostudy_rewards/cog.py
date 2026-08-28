import asyncio
from contextlib import suppress
import logging

from meta import LionBot, LionCog

from .data import GoStudyRewardsData
from .notifications import RewardNotificationWorker, inventory_url_from_base


logger = logging.getLogger(__name__)


class GoStudyRewardsCog(LionCog):
    """Loads the independent Go Study verified-hour reward data layer."""

    def __init__(self, bot: LionBot):
        self.bot = bot
        self.data = bot.db.load_registry(GoStudyRewardsData())
        self.notification_worker: RewardNotificationWorker | None = None
        self.notification_task: asyncio.Task | None = None

    async def cog_load(self):
        await self.data.init()
        if self.notification_task is not None:
            return

        parser = self.bot.config.config
        try:
            notifications_enabled = parser.getboolean(
                'GO_STUDY',
                'notifications_enabled',
                fallback=False,
            )
        except ValueError:
            notifications_enabled = False
            logger.warning(
                "Go Study reward notifications are disabled because the "
                "feature flag is invalid."
            )

        if not notifications_enabled:
            return

        configured_web_url = parser.get('GO_STUDY', 'web_url', fallback='')
        inventory_url = inventory_url_from_base(configured_web_url)
        if configured_web_url.strip() and inventory_url is None:
            logger.warning(
                "Go Study inventory link is omitted because web_url is invalid."
            )

        self.notification_worker = RewardNotificationWorker(
            self.bot,
            self.data,
            claimed_by=self.bot.shardname,
            inventory_url=inventory_url,
        )
        self.notification_task = asyncio.create_task(
            self.notification_worker.run(),
            name='Go Study reward notification worker',
        )
        self.notification_task.add_done_callback(self._notification_worker_done)

    def _notification_worker_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        if task.exception() is not None:
            logger.error("Go Study reward notification worker stopped unexpectedly.")

    async def cog_unload(self):
        task = self.notification_task
        worker = self.notification_worker
        self.notification_task = None
        self.notification_worker = None

        if task is None:
            return
        if worker is not None:
            worker.request_stop()

        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except asyncio.TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception:
            # The done callback has already emitted a fixed, redacted message.
            pass
