from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
import unicodedata
from typing import Any

from .data import (
    GoStudyGuildRegistryData,
    GuildEmojiSnapshot,
    GuildSnapshot,
    GuildStickerSnapshot,
)


logger = logging.getLogger(__name__)

_ASSET_HASH = re.compile(r'^(?:a_)?[0-9a-f]{1,128}$')


@dataclass(frozen=True, slots=True)
class GuildSyncSummary:
    attempted: int
    succeeded: int
    failed: int


def _positive_snowflake(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a positive Discord snowflake.')
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'{label} must be a positive Discord snowflake.') from exc
    if normalized <= 0 or normalized > 9223372036854775807:
        raise ValueError(f'{label} must be a positive Discord snowflake.')
    return normalized


def _clean_text(value: Any, limit: int, *, fallback: str | None = None) -> str | None:
    if value is None:
        return fallback
    text = str(value)
    text = ''.join(
        ' ' if unicodedata.category(character) == 'Cc' else character
        for character in text
    ).strip()
    if not text:
        return fallback
    return text[:limit]


def _asset_hash(asset: Any) -> str | None:
    if asset is None:
        return None
    key = getattr(asset, 'key', None)
    if not isinstance(key, str):
        return None
    normalized = key.strip().lower()
    return (
        normalized
        if len(normalized) <= 128 and _ASSET_HASH.fullmatch(normalized)
        else None
    )


def _optional_member_count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return count if 0 <= count <= 2147483647 else None


def _enum_value(value: Any) -> int:
    candidate = getattr(value, 'value', value)
    if isinstance(candidate, bool):
        return 0
    try:
        normalized = int(candidate)
    except (TypeError, ValueError, OverflowError):
        return 0
    return normalized if 0 <= normalized <= 32767 else 0


def _owned_by_guild(asset: Any, guildid: int) -> bool:
    owner = getattr(asset, 'guild_id', guildid)
    try:
        return int(owner) == guildid
    except (TypeError, ValueError, OverflowError):
        return False


def normalize_guild(guild: Any, *, observed_at: datetime) -> GuildSnapshot:
    """Normalize only public Discord guild metadata from discord.py objects."""
    guildid = _positive_snowflake(getattr(guild, 'id', None), 'guildid')
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError('Guild observation timestamps must be timezone-aware.')
    observed_at = observed_at.astimezone(timezone.utc)

    emojis: dict[int, GuildEmojiSnapshot] = {}
    for emoji in tuple(getattr(guild, 'emojis', ()) or ()):
        if not _owned_by_guild(emoji, guildid):
            continue
        try:
            emojiid = _positive_snowflake(getattr(emoji, 'id', None), 'emojiid')
        except ValueError:
            continue
        emojis[emojiid] = GuildEmojiSnapshot(
            emojiid=emojiid,
            guildid=guildid,
            name=_clean_text(getattr(emoji, 'name', None), 100, fallback='unnamed'),
            animated=bool(getattr(emoji, 'animated', False)),
            available=bool(getattr(emoji, 'available', True)),
            observed_at=observed_at,
        )

    stickers: dict[int, GuildStickerSnapshot] = {}
    for sticker in tuple(getattr(guild, 'stickers', ()) or ()):
        if not _owned_by_guild(sticker, guildid):
            continue
        try:
            stickerid = _positive_snowflake(getattr(sticker, 'id', None), 'stickerid')
        except ValueError:
            continue
        stickers[stickerid] = GuildStickerSnapshot(
            stickerid=stickerid,
            guildid=guildid,
            name=_clean_text(getattr(sticker, 'name', None), 100, fallback='unnamed'),
            description=_clean_text(getattr(sticker, 'description', None), 1000),
            format_type=_enum_value(getattr(sticker, 'format', None)),
            sticker_type=_enum_value(getattr(sticker, 'type', None)),
            available=bool(getattr(sticker, 'available', True)),
            observed_at=observed_at,
        )

    return GuildSnapshot(
        guildid=guildid,
        name=_clean_text(getattr(guild, 'name', None), 100, fallback='Unnamed guild'),
        icon_hash=_asset_hash(getattr(guild, 'icon', None)),
        banner_hash=_asset_hash(getattr(guild, 'banner', None)),
        description=_clean_text(getattr(guild, 'description', None), 120),
        member_count=_optional_member_count(getattr(guild, 'member_count', None)),
        emojis=tuple(emojis.values()),
        stickers=tuple(stickers.values()),
        observed_at=observed_at,
    )


class GuildRegistryService:
    """Authoritative guild reconciliation with per-guild serialization."""

    def __init__(
        self,
        data: GoStudyGuildRegistryData,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.data = data
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_observed_at: dict[int, datetime] = {}

    def _lock_for(self, guildid: int) -> asyncio.Lock:
        return self._locks.setdefault(guildid, asyncio.Lock())

    def _next_observed_at(self, guildid: int) -> datetime:
        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError('Guild registry clock must return timezone-aware timestamps.')
        observed_at = observed_at.astimezone(timezone.utc)
        previous = self._last_observed_at.get(guildid)
        if previous is not None and observed_at <= previous:
            observed_at = previous + timedelta(microseconds=1)
        self._last_observed_at[guildid] = observed_at
        return observed_at

    def snapshot(self, guild: Any) -> GuildSnapshot:
        guildid = _positive_snowflake(getattr(guild, 'id', None), 'guildid')
        return normalize_guild(guild, observed_at=self._next_observed_at(guildid))

    async def sync_snapshot(self, snapshot: GuildSnapshot) -> bool:
        async with self._lock_for(snapshot.guildid):
            return await self.data.sync_guild(snapshot)

    async def sync_guild(self, guild: Any) -> bool:
        return await self.sync_snapshot(self.snapshot(guild))

    async def mark_guild_inactive(self, guild: Any) -> bool:
        snapshot = self.snapshot(guild)
        async with self._lock_for(snapshot.guildid):
            return await self.data.mark_guild_inactive(snapshot)

    async def sync_all_guilds(self, guilds: Iterable[Any]) -> GuildSyncSummary:
        attempted = succeeded = failed = 0
        for guild in tuple(guilds):
            attempted += 1
            try:
                await self.sync_guild(guild)
            except Exception:
                failed += 1
                logger.exception(
                    'Go Study guild startup sync failed for guild ID %s.',
                    getattr(guild, 'id', 'unknown'),
                )
            else:
                succeeded += 1
        return GuildSyncSummary(attempted, succeeded, failed)
