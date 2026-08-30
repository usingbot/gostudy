from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from modules.gostudy_guild_registry.data import GuildSnapshot  # noqa: E402
from modules.gostudy_guild_registry.service import (  # noqa: E402
    GuildRegistryService,
    normalize_guild,
)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 30, tzinfo=timezone.utc)

    def __call__(self):
        value = self.value
        self.value += timedelta(seconds=1)
        return value


class Asset:
    def __init__(self, key, url='https://cdn.discordapp.com/should-not-persist.png'):
        self.key = key
        self.url = url


def emoji(
    emojiid,
    *,
    guildid=100,
    name='focus',
    animated=False,
    available=True,
):
    return SimpleNamespace(
        id=emojiid,
        guild_id=guildid,
        name=name,
        animated=animated,
        available=available,
    )


def sticker(
    stickerid,
    *,
    guildid=100,
    name='study',
    description=None,
    format_type=1,
    sticker_type=2,
    available=True,
):
    return SimpleNamespace(
        id=stickerid,
        guild_id=guildid,
        name=name,
        description=description,
        format=SimpleNamespace(value=format_type),
        type=SimpleNamespace(value=sticker_type),
        available=available,
    )


def guild(
    guildid=100,
    *,
    name='Study Hall',
    icon='abc123',
    banner=None,
    description='A public study server',
    member_count=42,
    emojis=(),
    stickers=(),
):
    return SimpleNamespace(
        id=guildid,
        name=name,
        icon=Asset(icon) if icon is not None else None,
        banner=Asset(banner) if banner is not None else None,
        description=description,
        member_count=member_count,
        emojis=tuple(emojis),
        stickers=tuple(stickers),
    )


class FakeGuildRegistryData:
    """In-memory mirror of the persistence contract for service unit tests."""

    def __init__(self):
        self.guilds = {}
        self.emojis = {}
        self.stickers = {}
        self.fail_guildids = set()
        self.delay_names = set()

    async def sync_guild(self, snapshot: GuildSnapshot):
        if snapshot.guildid in self.fail_guildids:
            raise RuntimeError('fixed fake database failure')
        if snapshot.name in self.delay_names:
            await asyncio.sleep(0.01)
        if not self._upsert_guild(snapshot, active=True):
            return False

        current_emojis = {item.emojiid for item in snapshot.emojis}
        for item in snapshot.emojis:
            row = self.emojis.get(item.emojiid)
            if row is None:
                first_seen_at = item.observed_at
            elif row['guildid'] != item.guildid or row['last_seen_at'] >= item.observed_at:
                continue
            else:
                first_seen_at = row['first_seen_at']
            self.emojis[item.emojiid] = {
                'emojiid': item.emojiid,
                'guildid': item.guildid,
                'name': item.name,
                'animated': item.animated,
                'available': item.available,
                'first_seen_at': first_seen_at,
                'last_seen_at': item.observed_at,
                'updated_at': item.observed_at,
            }
        for row in self.emojis.values():
            if row['guildid'] == snapshot.guildid and row['emojiid'] not in current_emojis:
                row['available'] = False
                row['updated_at'] = snapshot.observed_at

        current_stickers = {item.stickerid for item in snapshot.stickers}
        for item in snapshot.stickers:
            row = self.stickers.get(item.stickerid)
            if row is None:
                first_seen_at = item.observed_at
            elif row['guildid'] != item.guildid or row['last_seen_at'] >= item.observed_at:
                continue
            else:
                first_seen_at = row['first_seen_at']
            self.stickers[item.stickerid] = {
                'stickerid': item.stickerid,
                'guildid': item.guildid,
                'name': item.name,
                'description': item.description,
                'format_type': item.format_type,
                'sticker_type': item.sticker_type,
                'available': item.available,
                'first_seen_at': first_seen_at,
                'last_seen_at': item.observed_at,
                'updated_at': item.observed_at,
            }
        for row in self.stickers.values():
            if row['guildid'] == snapshot.guildid and row['stickerid'] not in current_stickers:
                row['available'] = False
                row['updated_at'] = snapshot.observed_at
        return True

    async def mark_guild_inactive(self, snapshot: GuildSnapshot):
        if not self._upsert_guild(snapshot, active=False):
            return False
        for rows in (self.emojis, self.stickers):
            for row in rows.values():
                if row['guildid'] == snapshot.guildid:
                    row['available'] = False
                    row['updated_at'] = snapshot.observed_at
        return True

    def _upsert_guild(self, snapshot, *, active):
        row = self.guilds.get(snapshot.guildid)
        if row is not None and row['last_synced_at'] >= snapshot.observed_at:
            return False
        first_seen_at = row['first_seen_at'] if row else snapshot.observed_at
        self.guilds[snapshot.guildid] = {
            'guildid': snapshot.guildid,
            'name': snapshot.name,
            'icon_hash': snapshot.icon_hash,
            'banner_hash': snapshot.banner_hash,
            'description': snapshot.description,
            'member_count': snapshot.member_count,
            'active': active,
            'first_seen_at': first_seen_at,
            'last_synced_at': snapshot.observed_at,
            'updated_at': snapshot.observed_at,
        }
        return True


class GuildRegistryServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = FakeGuildRegistryData()
        self.clock = Clock()
        self.service = GuildRegistryService(self.data, clock=self.clock)

    async def test_guild_first_sync_inserts_active_row(self):
        await self.service.sync_guild(guild())
        self.assertTrue(self.data.guilds[100]['active'])
        self.assertEqual(self.data.guilds[100]['name'], 'Study Hall')

    async def test_repeated_sync_is_idempotent(self):
        await self.service.sync_guild(guild(emojis=[emoji(200)]))
        first_seen = self.data.guilds[100]['first_seen_at']
        emoji_first_seen = self.data.emojis[200]['first_seen_at']
        await self.service.sync_guild(guild(emojis=[emoji(200)]))
        self.assertEqual(len(self.data.guilds), 1)
        self.assertEqual(len(self.data.emojis), 1)
        self.assertEqual(self.data.guilds[100]['first_seen_at'], first_seen)
        self.assertEqual(self.data.emojis[200]['first_seen_at'], emoji_first_seen)

    async def test_name_update_persists(self):
        await self.service.sync_guild(guild())
        await self.service.sync_guild(guild(name='Deep Work'))
        self.assertEqual(self.data.guilds[100]['name'], 'Deep Work')

    async def test_icon_change_persists_hash_only(self):
        await self.service.sync_guild(guild(icon='abc123'))
        await self.service.sync_guild(guild(icon='def456'))
        self.assertEqual(self.data.guilds[100]['icon_hash'], 'def456')

    async def test_icon_removal_persists_null(self):
        await self.service.sync_guild(guild(icon='abc123'))
        await self.service.sync_guild(guild(icon=None))
        self.assertIsNone(self.data.guilds[100]['icon_hash'])

    async def test_banner_change_persists(self):
        await self.service.sync_guild(guild(banner='111aaa'))
        await self.service.sync_guild(guild(banner='222bbb'))
        self.assertEqual(self.data.guilds[100]['banner_hash'], '222bbb')

    async def test_description_update_and_removal_persist(self):
        await self.service.sync_guild(guild(description='First'))
        await self.service.sync_guild(guild(description='Second'))
        self.assertEqual(self.data.guilds[100]['description'], 'Second')
        await self.service.sync_guild(guild(description=None))
        self.assertIsNone(self.data.guilds[100]['description'])

    async def test_member_count_update_persists(self):
        await self.service.sync_guild(guild(member_count=10))
        await self.service.sync_guild(guild(member_count=25))
        self.assertEqual(self.data.guilds[100]['member_count'], 25)

    async def test_emoji_first_sync(self):
        await self.service.sync_guild(guild(emojis=[emoji(200)]))
        self.assertEqual(self.data.emojis[200]['name'], 'focus')
        self.assertTrue(self.data.emojis[200]['available'])

    async def test_animated_emoji_persists(self):
        await self.service.sync_guild(guild(emojis=[emoji(200, animated=True)]))
        self.assertTrue(self.data.emojis[200]['animated'])

    async def test_emoji_rename_persists(self):
        await self.service.sync_guild(guild(emojis=[emoji(200, name='focus')]))
        await self.service.sync_guild(guild(emojis=[emoji(200, name='deepwork')]))
        self.assertEqual(self.data.emojis[200]['name'], 'deepwork')

    async def test_removed_emoji_becomes_unavailable(self):
        await self.service.sync_guild(guild(emojis=[emoji(200)]))
        await self.service.sync_guild(guild(emojis=[]))
        self.assertFalse(self.data.emojis[200]['available'])

    async def test_returning_emoji_becomes_available(self):
        await self.service.sync_guild(guild(emojis=[emoji(200)]))
        await self.service.sync_guild(guild(emojis=[]))
        await self.service.sync_guild(guild(emojis=[emoji(200, name='returned')]))
        self.assertTrue(self.data.emojis[200]['available'])
        self.assertEqual(self.data.emojis[200]['name'], 'returned')

    async def test_present_but_unavailable_emoji_is_explicit(self):
        await self.service.sync_guild(guild(emojis=[emoji(200, available=False)]))
        self.assertFalse(self.data.emojis[200]['available'])

    async def test_sticker_first_sync_and_description(self):
        await self.service.sync_guild(
            guild(stickers=[sticker(300, description='Keep studying')])
        )
        self.assertEqual(self.data.stickers[300]['name'], 'study')
        self.assertEqual(self.data.stickers[300]['description'], 'Keep studying')

    async def test_sticker_rename_persists(self):
        await self.service.sync_guild(guild(stickers=[sticker(300, name='study')]))
        await self.service.sync_guild(guild(stickers=[sticker(300, name='break')]))
        self.assertEqual(self.data.stickers[300]['name'], 'break')

    async def test_sticker_format_and_type_persist(self):
        await self.service.sync_guild(
            guild(stickers=[sticker(300, format_type=4, sticker_type=2)])
        )
        self.assertEqual(self.data.stickers[300]['format_type'], 4)
        self.assertEqual(self.data.stickers[300]['sticker_type'], 2)

    async def test_removed_sticker_becomes_unavailable(self):
        await self.service.sync_guild(guild(stickers=[sticker(300)]))
        await self.service.sync_guild(guild(stickers=[]))
        self.assertFalse(self.data.stickers[300]['available'])

    async def test_returning_sticker_becomes_available(self):
        await self.service.sync_guild(guild(stickers=[sticker(300)]))
        await self.service.sync_guild(guild(stickers=[]))
        await self.service.sync_guild(guild(stickers=[sticker(300, name='back')]))
        self.assertTrue(self.data.stickers[300]['available'])
        self.assertEqual(self.data.stickers[300]['name'], 'back')

    async def test_present_but_unavailable_sticker_is_explicit(self):
        await self.service.sync_guild(guild(stickers=[sticker(300, available=False)]))
        self.assertFalse(self.data.stickers[300]['available'])

    async def test_guild_leave_marks_inactive_and_preserves_identity(self):
        target = guild(name='Original')
        await self.service.sync_guild(target)
        await self.service.mark_guild_inactive(target)
        self.assertFalse(self.data.guilds[100]['active'])
        self.assertEqual(self.data.guilds[100]['name'], 'Original')

    async def test_guild_leave_makes_assets_unavailable(self):
        target = guild(emojis=[emoji(200)], stickers=[sticker(300)])
        await self.service.sync_guild(target)
        await self.service.mark_guild_inactive(target)
        self.assertFalse(self.data.emojis[200]['available'])
        self.assertFalse(self.data.stickers[300]['available'])

    async def test_rejoin_activates_and_reconciles(self):
        target = guild(emojis=[emoji(200)], stickers=[sticker(300)])
        await self.service.sync_guild(target)
        await self.service.mark_guild_inactive(target)
        await self.service.sync_guild(target)
        self.assertTrue(self.data.guilds[100]['active'])
        self.assertTrue(self.data.emojis[200]['available'])
        self.assertTrue(self.data.stickers[300]['available'])

    async def test_startup_reconciles_every_current_guild(self):
        summary = await self.service.sync_all_guilds([guild(100), guild(101)])
        self.assertEqual(summary.attempted, 2)
        self.assertEqual(summary.succeeded, 2)
        self.assertEqual(set(self.data.guilds), {100, 101})

    async def test_two_guilds_do_not_interfere(self):
        await self.service.sync_guild(guild(100, emojis=[emoji(200, guildid=100)]))
        await self.service.sync_guild(guild(101, emojis=[emoji(201, guildid=101)]))
        await self.service.sync_guild(guild(100, emojis=[]))
        self.assertFalse(self.data.emojis[200]['available'])
        self.assertTrue(self.data.emojis[201]['available'])

    async def test_concurrent_same_guild_sync_cannot_regress_newer_state(self):
        base = datetime(2026, 8, 30, tzinfo=timezone.utc)
        older = normalize_guild(
            guild(name='older', emojis=[]),
            observed_at=base,
        )
        newer = normalize_guild(
            guild(name='newer', emojis=[emoji(200)]),
            observed_at=base + timedelta(seconds=1),
        )
        await asyncio.gather(
            self.service.sync_snapshot(newer),
            self.service.sync_snapshot(older),
        )
        self.assertEqual(self.data.guilds[100]['name'], 'newer')
        self.assertTrue(self.data.emojis[200]['available'])

    async def test_startup_failure_does_not_prevent_other_guilds(self):
        self.data.fail_guildids.add(100)
        with self.assertLogs(
            'modules.gostudy_guild_registry.service',
            level='ERROR',
        ):
            summary = await self.service.sync_all_guilds([guild(100), guild(101)])
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.succeeded, 1)
        self.assertIn(101, self.data.guilds)


class GuildRegistryNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.observed_at = datetime(2026, 8, 30, tzinfo=timezone.utc)

    def test_cdn_urls_and_image_bytes_are_not_normalized(self):
        target = guild(icon='a_abc123', banner='def456')
        snapshot = normalize_guild(target, observed_at=self.observed_at)
        self.assertEqual(snapshot.icon_hash, 'a_abc123')
        self.assertEqual(snapshot.banner_hash, 'def456')
        self.assertNotIn('cdn.discordapp.com', repr(snapshot))
        self.assertFalse(hasattr(snapshot, 'icon_url'))

    def test_url_shaped_asset_key_is_rejected(self):
        target = guild(icon=None)
        target.icon = Asset('https://cdn.discordapp.com/icons/100/hash.png')
        snapshot = normalize_guild(target, observed_at=self.observed_at)
        self.assertIsNone(snapshot.icon_hash)

    def test_member_list_is_never_read_or_persisted(self):
        class GuardedGuild:
            id = 100
            name = 'Guarded'
            icon = None
            banner = None
            description = None
            member_count = 1
            emojis = ()
            stickers = ()

            @property
            def members(self):
                raise AssertionError('member list must not be read')

        snapshot = normalize_guild(GuardedGuild(), observed_at=self.observed_at)
        self.assertEqual(snapshot.member_count, 1)
        self.assertFalse(hasattr(snapshot, 'members'))

    def test_malformed_and_oversized_metadata_is_bounded(self):
        target = guild(
            name='x\x00' * 100,
            description='d' * 500,
            member_count=-1,
            emojis=[emoji(-1), emoji(200, name='e' * 500)],
            stickers=[sticker(300, name='s' * 500, description='z' * 2000)],
        )
        snapshot = normalize_guild(target, observed_at=self.observed_at)
        self.assertLessEqual(len(snapshot.name), 100)
        self.assertNotIn('\x00', snapshot.name)
        self.assertEqual(len(snapshot.description), 120)
        self.assertIsNone(snapshot.member_count)
        self.assertEqual([item.emojiid for item in snapshot.emojis], [200])
        self.assertEqual(len(snapshot.emojis[0].name), 100)
        self.assertEqual(len(snapshot.stickers[0].name), 100)
        self.assertEqual(len(snapshot.stickers[0].description), 1000)

    def test_cross_guild_assets_are_ignored(self):
        snapshot = normalize_guild(
            guild(
                emojis=[emoji(200, guildid=999)],
                stickers=[sticker(300, guildid=999)],
            ),
            observed_at=self.observed_at,
        )
        self.assertEqual(snapshot.emojis, ())
        self.assertEqual(snapshot.stickers, ())

    def test_invalid_guild_snowflake_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'positive Discord snowflake'):
            normalize_guild(guild(False), observed_at=self.observed_at)

    def test_no_private_content_fields_exist(self):
        snapshot = normalize_guild(guild(), observed_at=self.observed_at)
        forbidden = {
            'members',
            'users',
            'messages',
            'channels',
            'roles',
            'invites',
            'audit_logs',
        }
        self.assertTrue(forbidden.isdisjoint(snapshot.__dataclass_fields__))


if __name__ == '__main__':
    unittest.main()
