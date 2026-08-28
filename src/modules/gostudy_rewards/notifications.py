"""Durable, leased delivery of private Go Study reward notifications.

Discord delivery is intentionally at-least-once. If Discord accepts a DM and
the process exits before PostgreSQL records the acknowledgement, lease recovery
can cause the same reward notification to be delivered again.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import discord

from .data import GoStudyRewardsData


POLL_INTERVAL_SECONDS = 5
LEASE_SECONDS = 5 * 60
MAX_REWARDS_PER_DM = 10
MAX_ATTEMPTS = 6
MAX_DISPLAY_NAME_LENGTH = 80

ITEM_EMOJI = {
    'coffee': '☕',
    'books': '📚',
    'moon': '🌙',
    'study-star': '⭐',
    'verified-hour-token': '🎁',
}
FALLBACK_EMOJI = '🎁'
FALLBACK_DISPLAY_NAME = 'Verified Hour Reward'

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscordFailure:
    code: str
    terminal: bool


def inventory_url_from_base(web_url: str | None) -> str | None:
    """Return a safe inventory URL without ever logging the supplied value."""
    if not web_url:
        return None

    candidate = web_url.strip().rstrip('/')
    if not candidate or any(ord(character) < 32 for character in candidate):
        return None

    try:
        parsed = urlsplit(candidate)
        # Accessing port validates a malformed numeric/range value.
        _ = parsed.port
    except ValueError:
        return None

    if (
        parsed.scheme.lower() not in {'http', 'https'}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None

    path = parsed.path.rstrip('/') + '/inventory'
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, '', ''))


def safe_display_name(value: object) -> str:
    """Collapse, escape, and bound an untrusted catalog display name."""
    if not isinstance(value, str):
        return FALLBACK_DISPLAY_NAME
    collapsed = ' '.join(value.split())
    if not collapsed:
        return FALLBACK_DISPLAY_NAME
    escaped = discord.utils.escape_markdown(collapsed)
    if len(escaped) > MAX_DISPLAY_NAME_LENGTH:
        escaped = escaped[:MAX_DISPLAY_NAME_LENGTH - 1].rstrip() + '…'
    return escaped or FALLBACK_DISPLAY_NAME


def merge_claim_details(
    claimed: Sequence[Mapping[str, Any]],
    fetched: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Preserve every claimed notification, using safe catalog fallbacks."""
    fetched_by_notification = {
        row['notificationid']: row for row in fetched
        if row.get('notificationid') is not None
    }
    merged = []
    for claim in claimed:
        row = dict(claim)
        row.update(fetched_by_notification.get(claim['notificationid'], {}))
        row.setdefault('item_key', None)
        row.setdefault('display_name', None)
        merged.append(row)
    return merged


def format_reward_message(details: Sequence[Mapping[str, Any]]) -> str:
    """Build one bounded DM for all independently tracked claim rows."""
    reward_count = len(details)
    if reward_count == 1:
        heading = '🎁 Reward earned!'
        milestone = 'Your verified study time reached another 60-minute milestone.'
    else:
        heading = f'🎁 {reward_count} rewards earned!'
        milestone = (
            f'Your verified study time crossed {reward_count} hourly milestones.'
        )

    grouped: OrderedDict[tuple[object, str], int] = OrderedDict()
    for detail in details:
        item_key = detail.get('item_key')
        display_name = safe_display_name(detail.get('display_name'))
        group_key = (item_key, display_name)
        grouped[group_key] = grouped.get(group_key, 0) + 1

    item_lines = []
    for (item_key, display_name), quantity in grouped.items():
        emoji = ITEM_EMOJI.get(item_key, FALLBACK_EMOJI)
        suffix = f' ×{quantity}' if quantity > 1 else ''
        item_lines.append(f'{emoji} {display_name}{suffix}')

    return '\n\n'.join((
        heading,
        milestone,
        'You earned:\n' + '\n'.join(item_lines),
        (
            'Verified time counts time while your camera is on in supported '
            'study rooms.'
        ),
    ))


def inventory_link_view(inventory_url: str | None) -> discord.ui.View | None:
    """Build a URL-only view; it has no callback or persistent custom ID."""
    if inventory_url is None:
        return None
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label='View Inventory', url=inventory_url))
    return view


def classify_discord_failure(error: BaseException) -> DiscordFailure:
    """Map Discord/network exceptions to fixed, non-sensitive result codes."""
    if isinstance(error, discord.Forbidden):
        return DiscordFailure('dm_forbidden', terminal=True)
    if isinstance(error, discord.NotFound):
        return DiscordFailure('user_not_found', terminal=True)
    if isinstance(error, discord.HTTPException):
        status = error.status
        if status == 429:
            return DiscordFailure('discord_rate_limited', terminal=False)
        if 400 <= status < 500:
            return DiscordFailure('discord_http_4xx', terminal=True)
        if status >= 500:
            return DiscordFailure('discord_server_error', terminal=False)
        return DiscordFailure('discord_temporary', terminal=False)
    if isinstance(error, OSError):
        return DiscordFailure('network_error', terminal=False)
    return DiscordFailure('discord_temporary', terminal=False)


class RewardNotificationWorker:
    """Poll and deliver one transactionally claimed user batch at a time."""

    def __init__(
        self,
        bot,
        data: GoStudyRewardsData,
        *,
        claimed_by: str,
        inventory_url: str | None,
        log: logging.Logger = logger,
    ):
        self.bot = bot
        self.data = data
        self.claimed_by = claimed_by[:128]
        self.inventory_url = inventory_url
        self.log = log
        self._stop_event = asyncio.Event()

    def request_stop(self) -> None:
        """Prevent the next claim and wake the polling delay."""
        self._stop_event.set()

    async def run(self) -> None:
        """Wait for Discord readiness, then poll until shutdown is requested."""
        await self.bot.wait_until_ready()
        self.log.info("Reward notification worker is ready.")
        while not self._stop_event.is_set():
            await self.process_once()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=POLL_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                pass

    async def process_once(self) -> bool:
        """Claim and process at most one user's batch without a long DB transaction."""
        if self._stop_event.is_set():
            return False

        claim_token = uuid4()
        try:
            claimed = await self.data.claim_notification_batch(
                claim_token,
                self.claimed_by,
                batch_size=MAX_REWARDS_PER_DM,
                lease_seconds=LEASE_SECONDS,
                max_attempts=MAX_ATTEMPTS,
            )
        except Exception:
            self.log.warning("Reward notification database claim failed.")
            return False

        if not claimed:
            return False

        userid = claimed[0]['userid']
        notification_ids = [row['notificationid'] for row in claimed]
        hour_reward_ids = [row['hour_rewardid'] for row in claimed]
        attempts = [row['attempt_count'] for row in claimed]

        try:
            fetched = await self.data.fetch_notification_claim_details(claim_token)
        except Exception:
            # Do not invent a result. The untouched processing rows will be
            # recovered when their lease expires.
            self.log.warning(
                "Reward notification database detail lookup failed; "
                "userid=%s notification_ids=%s",
                userid,
                notification_ids,
            )
            return True

        details = merge_claim_details(claimed, fetched)
        message = format_reward_message(details)
        view = inventory_link_view(self.inventory_url)

        try:
            user = self.bot.get_user(userid)
            if user is None:
                user = await self.bot.fetch_user(userid)
            await user.send(
                message,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.HTTPException, OSError) as error:
            failure = classify_discord_failure(error)
            await self._record_failure(
                claim_token,
                failure,
                userid=userid,
                notification_ids=notification_ids,
                hour_reward_ids=hour_reward_ids,
                attempts=attempts,
            )
            return True
        except Exception:
            await self._record_failure(
                claim_token,
                DiscordFailure('discord_temporary', terminal=False),
                userid=userid,
                notification_ids=notification_ids,
                hour_reward_ids=hour_reward_ids,
                attempts=attempts,
            )
            return True

        try:
            updated = await self.data.mark_notification_claim_delivered(claim_token)
        except Exception:
            self.log.warning(
                "Reward notification database delivery acknowledgement failed; "
                "userid=%s notification_ids=%s",
                userid,
                notification_ids,
            )
            return True

        self.log.info(
            "Reward notification delivery acknowledged; userid=%s "
            "notification_ids=%s hour_reward_ids=%s updated=%s",
            userid,
            notification_ids,
            hour_reward_ids,
            updated,
        )
        return True

    async def _record_failure(
        self,
        claim_token,
        failure: DiscordFailure,
        *,
        userid: int,
        notification_ids: list[int],
        hour_reward_ids: list[int],
        attempts: list[int],
    ) -> None:
        try:
            if failure.terminal:
                updated = await self.data.fail_notification_claim(
                    claim_token,
                    failure.code,
                )
            else:
                updated = await self.data.retry_notification_claim(
                    claim_token,
                    failure.code,
                    max_attempts=MAX_ATTEMPTS,
                )
        except Exception:
            # Leave the lease intact so recovery, rather than an invented
            # acknowledgement, determines the next state.
            self.log.warning(
                "Reward notification database failure acknowledgement failed; "
                "userid=%s notification_ids=%s",
                userid,
                notification_ids,
            )
            return

        self.log.info(
            "Reward notification delivery failed; userid=%s notification_ids=%s "
            "hour_reward_ids=%s attempts=%s failure_code=%s updated=%s",
            userid,
            notification_ids,
            hour_reward_ids,
            attempts,
            failure.code,
            updated,
        )
