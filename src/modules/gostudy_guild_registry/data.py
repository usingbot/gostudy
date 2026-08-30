from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg import AsyncCursor
from psycopg.rows import dict_row

from data import Registry


@dataclass(frozen=True, slots=True)
class GuildEmojiSnapshot:
    emojiid: int
    guildid: int
    name: str
    animated: bool
    available: bool
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class GuildStickerSnapshot:
    stickerid: int
    guildid: int
    name: str
    description: str | None
    format_type: int
    sticker_type: int
    available: bool
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class GuildSnapshot:
    guildid: int
    name: str
    icon_hash: str | None
    banner_hash: str | None
    description: str | None
    member_count: int | None
    emojis: tuple[GuildEmojiSnapshot, ...]
    stickers: tuple[GuildStickerSnapshot, ...]
    observed_at: datetime


def guild_registry_cursor(conn):
    return AsyncCursor(conn, row_factory=dict_row)


class GoStudyGuildRegistryData(Registry, name='gostudy_guild_registry'):
    """Transactional persistence boundary for Discord guild metadata."""

    async def _upsert_guild(self, cursor, snapshot: GuildSnapshot, *, active: bool):
        query = """
            INSERT INTO public.gostudy_guilds (
              guildid,
              name,
              icon_hash,
              banner_hash,
              description,
              member_count,
              active,
              first_seen_at,
              last_synced_at,
              updated_at
            ) VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (guildid) DO UPDATE SET
              name = EXCLUDED.name,
              icon_hash = EXCLUDED.icon_hash,
              banner_hash = EXCLUDED.banner_hash,
              description = EXCLUDED.description,
              member_count = EXCLUDED.member_count,
              active = EXCLUDED.active,
              last_synced_at = EXCLUDED.last_synced_at,
              updated_at = EXCLUDED.updated_at
            WHERE
              public.gostudy_guilds.last_synced_at < EXCLUDED.last_synced_at
            RETURNING guildid
        """
        observed_at = snapshot.observed_at
        await cursor.execute(
            query,
            (
                snapshot.guildid,
                snapshot.name,
                snapshot.icon_hash,
                snapshot.banner_hash,
                snapshot.description,
                snapshot.member_count,
                active,
                observed_at,
                observed_at,
                observed_at,
            ),
        )
        return await cursor.fetchone()

    async def _sync_emojis(self, cursor, snapshot: GuildSnapshot) -> None:
        upsert = """
            INSERT INTO public.gostudy_guild_emojis (
              emojiid,
              guildid,
              name,
              animated,
              available,
              first_seen_at,
              last_seen_at,
              updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (emojiid) DO UPDATE SET
              name = EXCLUDED.name,
              animated = EXCLUDED.animated,
              available = EXCLUDED.available,
              last_seen_at = EXCLUDED.last_seen_at,
              updated_at = EXCLUDED.updated_at
            WHERE
              public.gostudy_guild_emojis.guildid = EXCLUDED.guildid
              AND public.gostudy_guild_emojis.last_seen_at < EXCLUDED.last_seen_at
        """
        if snapshot.emojis:
            await cursor.executemany(
                upsert,
                [
                    (
                        emoji.emojiid,
                        emoji.guildid,
                        emoji.name,
                        emoji.animated,
                        emoji.available,
                        emoji.observed_at,
                        emoji.observed_at,
                        emoji.observed_at,
                    )
                    for emoji in snapshot.emojis
                ],
            )

        await cursor.execute(
            """
            UPDATE public.gostudy_guild_emojis
            SET available = FALSE, updated_at = %s
            WHERE
              guildid = %s
              AND available
              AND NOT (emojiid = ANY(%s::BIGINT[]))
            """,
            (
                snapshot.observed_at,
                snapshot.guildid,
                [emoji.emojiid for emoji in snapshot.emojis],
            ),
        )

    async def _sync_stickers(self, cursor, snapshot: GuildSnapshot) -> None:
        upsert = """
            INSERT INTO public.gostudy_guild_stickers (
              stickerid,
              guildid,
              name,
              description,
              format_type,
              sticker_type,
              available,
              first_seen_at,
              last_seen_at,
              updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (stickerid) DO UPDATE SET
              name = EXCLUDED.name,
              description = EXCLUDED.description,
              format_type = EXCLUDED.format_type,
              sticker_type = EXCLUDED.sticker_type,
              available = EXCLUDED.available,
              last_seen_at = EXCLUDED.last_seen_at,
              updated_at = EXCLUDED.updated_at
            WHERE
              public.gostudy_guild_stickers.guildid = EXCLUDED.guildid
              AND public.gostudy_guild_stickers.last_seen_at < EXCLUDED.last_seen_at
        """
        if snapshot.stickers:
            await cursor.executemany(
                upsert,
                [
                    (
                        sticker.stickerid,
                        sticker.guildid,
                        sticker.name,
                        sticker.description,
                        sticker.format_type,
                        sticker.sticker_type,
                        sticker.available,
                        sticker.observed_at,
                        sticker.observed_at,
                        sticker.observed_at,
                    )
                    for sticker in snapshot.stickers
                ],
            )

        await cursor.execute(
            """
            UPDATE public.gostudy_guild_stickers
            SET available = FALSE, updated_at = %s
            WHERE
              guildid = %s
              AND available
              AND NOT (stickerid = ANY(%s::BIGINT[]))
            """,
            (
                snapshot.observed_at,
                snapshot.guildid,
                [sticker.stickerid for sticker in snapshot.stickers],
            ),
        )

    async def sync_guild(self, snapshot: GuildSnapshot) -> bool:
        """Atomically apply one authoritative guild snapshot when it is newest."""
        async with self._conn.connection() as conn:
            async with conn.transaction():
                async with guild_registry_cursor(conn) as cursor:
                    row = await self._upsert_guild(cursor, snapshot, active=True)
                    if row is None:
                        return False
                    await self._sync_emojis(cursor, snapshot)
                    await self._sync_stickers(cursor, snapshot)
        return True

    async def mark_guild_inactive(self, snapshot: GuildSnapshot) -> bool:
        """Preserve a departed guild and make all of its assets inaccessible."""
        async with self._conn.connection() as conn:
            async with conn.transaction():
                async with guild_registry_cursor(conn) as cursor:
                    row = await self._upsert_guild(cursor, snapshot, active=False)
                    if row is None:
                        return False
                    await cursor.execute(
                        """
                        UPDATE public.gostudy_guild_emojis
                        SET available = FALSE, updated_at = %s
                        WHERE guildid = %s AND available
                        """,
                        (snapshot.observed_at, snapshot.guildid),
                    )
                    await cursor.execute(
                        """
                        UPDATE public.gostudy_guild_stickers
                        SET available = FALSE, updated_at = %s
                        WHERE guildid = %s AND available
                        """,
                        (snapshot.observed_at, snapshot.guildid),
                    )
        return True
