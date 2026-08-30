"""Disposable PostgreSQL tests for the Go Study guild registry.

The tests are deliberately double-gated and delete their own fixtures:

GOSTUDY_GUILD_TEST_DATABASE_URL=... \
GOSTUDY_GUILD_TEST_ALLOW_WRITES=1 \
  python -m unittest tests/integration/test_gostudy_guild_registry.py

Never point these variables at the real lion_data database.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import random
import sys
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from data.connector import Connector  # noqa: E402
from modules.gostudy_guild_registry.data import (  # noqa: E402
    GoStudyGuildRegistryData,
    GuildEmojiSnapshot,
    GuildSnapshot,
    GuildStickerSnapshot,
)


TEST_DATABASE_URL = os.environ.get('GOSTUDY_GUILD_TEST_DATABASE_URL', '')
WRITES_ALLOWED = os.environ.get('GOSTUDY_GUILD_TEST_ALLOW_WRITES') == '1'
MIGRATION_ADMIN_URL = os.environ.get('GOSTUDY_GUILD_MIGRATION_TEST_ADMIN_URL', '')
MIGRATION_WRITES_ALLOWED = (
    os.environ.get('GOSTUDY_GUILD_MIGRATION_TEST_ALLOW_WRITES') == '1'
)


@unittest.skipUnless(
    TEST_DATABASE_URL and WRITES_ALLOWED,
    'requires an explicitly authorized disposable PostgreSQL schema v22 database',
)
class GuildRegistryPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connector = Connector(TEST_DATABASE_URL)
        self.pool_context = self.connector.open()
        await self.pool_context.__aenter__()
        self.data = GoStudyGuildRegistryData()
        self.data.bind(self.connector)
        await self.data.init()

        seed = random.SystemRandom().randrange(10**12)
        self.guildid = 800000000000000000 + seed
        self.other_guildid = self.guildid + 1
        self.base_time = datetime.now(timezone.utc) - timedelta(days=1)

        async with self.connector.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    'SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1'
                )
                row = await cursor.fetchone()
        if row is None or row['version'] != 22:
            await self.pool_context.__aexit__(None, None, None)
            raise unittest.SkipTest('disposable database is not schema version 22')

    async def asyncTearDown(self):
        async with self.connector.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    'DELETE FROM public.gostudy_guilds WHERE guildid = ANY(%s::BIGINT[])',
                    ([self.guildid, self.other_guildid],),
                )
        await self.pool_context.__aexit__(None, None, None)

    def snapshot(
        self,
        *,
        guildid=None,
        seconds=0,
        name='Study Hall',
        icon_hash='abc123',
        emojis=(),
        stickers=(),
    ):
        guildid = guildid or self.guildid
        observed_at = self.base_time + timedelta(seconds=seconds)
        return GuildSnapshot(
            guildid=guildid,
            name=name,
            icon_hash=icon_hash,
            banner_hash=None,
            description='A public study server',
            member_count=42,
            emojis=tuple(
                GuildEmojiSnapshot(
                    emojiid=item[0],
                    guildid=guildid,
                    name=item[1],
                    animated=item[2],
                    available=True,
                    observed_at=observed_at,
                )
                for item in emojis
            ),
            stickers=tuple(
                GuildStickerSnapshot(
                    stickerid=item[0],
                    guildid=guildid,
                    name=item[1],
                    description=item[2],
                    format_type=1,
                    sticker_type=2,
                    available=True,
                    observed_at=observed_at,
                )
                for item in stickers
            ),
            observed_at=observed_at,
        )

    async def fetchone(self, query, params=()):
        async with self.connector.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, params)
                return await cursor.fetchone()

    async def test_full_sync_is_idempotent_and_updates_metadata(self):
        first = self.snapshot(
            emojis=[(self.guildid + 10, 'focus', True)],
            stickers=[(self.guildid + 20, 'study', 'Keep going')],
        )
        second = self.snapshot(
            seconds=1,
            name='Deep Work',
            icon_hash=None,
            emojis=[(self.guildid + 10, 'deepwork', True)],
            stickers=[(self.guildid + 20, 'break', None)],
        )
        self.assertTrue(await self.data.sync_guild(first))
        self.assertTrue(await self.data.sync_guild(second))

        row = await self.fetchone(
            'SELECT * FROM public.gostudy_guilds WHERE guildid = %s',
            (self.guildid,),
        )
        self.assertEqual(row['name'], 'Deep Work')
        self.assertIsNone(row['icon_hash'])
        self.assertEqual(row['first_seen_at'], first.observed_at)
        self.assertEqual(row['last_synced_at'], second.observed_at)

        emoji_row = await self.fetchone(
            'SELECT * FROM public.gostudy_guild_emojis WHERE emojiid = %s',
            (self.guildid + 10,),
        )
        sticker_row = await self.fetchone(
            'SELECT * FROM public.gostudy_guild_stickers WHERE stickerid = %s',
            (self.guildid + 20,),
        )
        self.assertEqual(emoji_row['name'], 'deepwork')
        self.assertTrue(emoji_row['animated'])
        self.assertEqual(sticker_row['name'], 'break')
        self.assertIsNone(sticker_row['description'])

    async def test_disappeared_and_returning_assets_toggle_availability(self):
        emojiid = self.guildid + 10
        stickerid = self.guildid + 20
        await self.data.sync_guild(self.snapshot(
            emojis=[(emojiid, 'focus', False)],
            stickers=[(stickerid, 'study', None)],
        ))
        await self.data.sync_guild(self.snapshot(seconds=1))
        unavailable = await self.fetchone(
            """
            SELECT
              (SELECT available FROM public.gostudy_guild_emojis WHERE emojiid = %s)
                AS emoji_available,
              (SELECT available FROM public.gostudy_guild_stickers WHERE stickerid = %s)
                AS sticker_available
            """,
            (emojiid, stickerid),
        )
        self.assertFalse(unavailable['emoji_available'])
        self.assertFalse(unavailable['sticker_available'])

        await self.data.sync_guild(self.snapshot(
            seconds=2,
            emojis=[(emojiid, 'returned', False)],
            stickers=[(stickerid, 'returned', 'Again')],
        ))
        available = await self.fetchone(
            """
            SELECT
              (SELECT available FROM public.gostudy_guild_emojis WHERE emojiid = %s)
                AS emoji_available,
              (SELECT available FROM public.gostudy_guild_stickers WHERE stickerid = %s)
                AS sticker_available
            """,
            (emojiid, stickerid),
        )
        self.assertTrue(available['emoji_available'])
        self.assertTrue(available['sticker_available'])

    async def test_leave_and_rejoin_preserve_history(self):
        initial = self.snapshot(
            emojis=[(self.guildid + 10, 'focus', False)],
            stickers=[(self.guildid + 20, 'study', None)],
        )
        await self.data.sync_guild(initial)
        await self.data.mark_guild_inactive(self.snapshot(seconds=1))
        inactive = await self.fetchone(
            'SELECT active, first_seen_at FROM public.gostudy_guilds WHERE guildid = %s',
            (self.guildid,),
        )
        self.assertFalse(inactive['active'])

        await self.data.sync_guild(self.snapshot(
            seconds=2,
            emojis=[(self.guildid + 10, 'focus', False)],
            stickers=[(self.guildid + 20, 'study', None)],
        ))
        active = await self.fetchone(
            'SELECT active, first_seen_at FROM public.gostudy_guilds WHERE guildid = %s',
            (self.guildid,),
        )
        self.assertTrue(active['active'])
        self.assertEqual(active['first_seen_at'], inactive['first_seen_at'])

    async def test_stale_concurrent_snapshot_cannot_regress_newer_state(self):
        older = self.snapshot(seconds=1, name='Older')
        newer = self.snapshot(
            seconds=2,
            name='Newer',
            emojis=[(self.guildid + 10, 'new', False)],
        )
        results = await asyncio.gather(
            self.data.sync_guild(newer),
            self.data.sync_guild(older),
        )
        self.assertIn(True, results)
        row = await self.fetchone(
            'SELECT name FROM public.gostudy_guilds WHERE guildid = %s',
            (self.guildid,),
        )
        emoji_row = await self.fetchone(
            'SELECT available FROM public.gostudy_guild_emojis WHERE emojiid = %s',
            (self.guildid + 10,),
        )
        self.assertEqual(row['name'], 'Newer')
        self.assertTrue(emoji_row['available'])

    async def test_two_guilds_do_not_interfere(self):
        await self.data.sync_guild(self.snapshot(
            guildid=self.guildid,
            emojis=[(self.guildid + 10, 'one', False)],
        ))
        await self.data.sync_guild(self.snapshot(
            guildid=self.other_guildid,
            emojis=[(self.other_guildid + 10, 'two', False)],
        ))
        await self.data.sync_guild(self.snapshot(seconds=1, guildid=self.guildid))
        other = await self.fetchone(
            'SELECT available FROM public.gostudy_guild_emojis WHERE emojiid = %s',
            (self.other_guildid + 10,),
        )
        self.assertTrue(other['available'])


@unittest.skipUnless(
    MIGRATION_ADMIN_URL and MIGRATION_WRITES_ALLOWED,
    'requires an explicitly authorized disposable PostgreSQL admin target',
)
class GuildRegistryMigrationPostgresTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = (
            'public.gostudy_guilds',
            'public.gostudy_guild_emojis',
            'public.gostudy_guild_stickers',
        )
        cls.schema_v22 = (ROOT / 'data/schema.sql').read_text(encoding='utf-8')

        privilege_marker = (
            '-- Go Study Discord guild registry runtime privileges {{{'
        )
        privilege_start = cls.schema_v22.index(privilege_marker)
        privilege_end = cls.schema_v22.index(
            '-- }}}', privilege_start
        ) + len('-- }}}')
        cls.schema_v21 = (
            cls.schema_v22[:privilege_start] + cls.schema_v22[privilege_end:]
        ).replace(
            "INSERT INTO VersionHistory (version, author) VALUES (22, 'Initial Creation');",
            "INSERT INTO VersionHistory (version, author) VALUES (21, 'Initial Creation');",
            1,
        )

        registry_marker = '-- Go Study Discord guild registry {{{'
        registry_start = cls.schema_v21.index(registry_marker)
        registry_end = cls.schema_v21.index(
            '-- }}}', registry_start
        ) + len('-- }}}')
        cls.schema_v20 = (
            cls.schema_v21[:registry_start] + cls.schema_v21[registry_end:]
        ).replace(
            "INSERT INTO VersionHistory (version, author) VALUES (21, 'Initial Creation');",
            "INSERT INTO VersionHistory (version, author) VALUES (20, 'Initial Creation');",
            1,
        )
        cls.migration_v20_v21 = (
            ROOT / 'data/migration/v20-v21/migration.sql'
        ).read_text(encoding='utf-8')
        cls.migration_v21_v22 = (
            ROOT / 'data/migration/v21-v22/migration.sql'
        ).read_text(encoding='utf-8')

    async def asyncSetUp(self):
        self.database_name = f'gostudy_guild_registry_{uuid4().hex}'
        self.admin = await psycopg.AsyncConnection.connect(
            MIGRATION_ADMIN_URL,
            autocommit=True,
        )
        async with self.admin.cursor() as cursor:
            await cursor.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname = 'lion'"
            )
            role = await cursor.fetchone()
            if role is None:
                await self.admin.close()
                raise unittest.SkipTest(
                    'migration target does not define the runtime role lion'
                )
            if role[0]:
                await self.admin.close()
                raise unittest.SkipTest(
                    'runtime role lion must not be a superuser for ACL verification'
                )
            await cursor.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(
                    sql.Identifier(self.database_name)
                )
            )
        self.database_url = make_conninfo(
            MIGRATION_ADMIN_URL,
            dbname=self.database_name,
        )

    async def assert_privilege_matrix(self, cursor):
        expected = {
            'SELECT': True,
            'INSERT': True,
            'UPDATE': True,
            'DELETE': False,
            'TRUNCATE': False,
        }
        for table in self.tables:
            for privilege, allowed in expected.items():
                await cursor.execute(
                    'SELECT has_table_privilege(%s, %s, %s)',
                    ('lion', table, privilege),
                )
                self.assertEqual(
                    (await cursor.fetchone())[0],
                    allowed,
                    f'lion {privilege} on {table}',
                )

            for privilege in ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'):
                await cursor.execute(
                    """
                    SELECT EXISTS (
                      SELECT 1
                      FROM pg_class AS relation
                      JOIN pg_namespace AS namespace
                        ON namespace.oid = relation.relnamespace
                      CROSS JOIN LATERAL aclexplode(
                        COALESCE(
                          relation.relacl,
                          acldefault('r', relation.relowner)
                        )
                      ) AS acl
                      WHERE namespace.nspname = 'public'
                        AND relation.relname = %s
                        AND acl.grantee = 0
                        AND acl.privilege_type = %s
                    )
                    """,
                    (table.removeprefix('public.'), privilege),
                )
                self.assertFalse(
                    (await cursor.fetchone())[0],
                    f'PUBLIC unexpectedly has {privilege} on {table}',
                )

            await cursor.execute(
                """
                SELECT pg_get_userbyid(relation.relowner)
                FROM pg_class AS relation
                WHERE relation.oid = %s::regclass
                """,
                (table,),
            )
            self.assertNotEqual((await cursor.fetchone())[0], 'lion')

    async def exercise_runtime_as_lion(self):
        runtime_url = make_conninfo(
            self.database_url,
            options='-c role=lion',
        )
        connector = Connector(runtime_url)
        async with connector.open():
            data = GoStudyGuildRegistryData()
            data.bind(connector)
            await data.init()

            guildid = 850000000000000000 + random.SystemRandom().randrange(10**12)
            observed_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            snapshot = GuildSnapshot(
                guildid=guildid,
                name='Migration runtime check',
                icon_hash=None,
                banner_hash=None,
                description=None,
                member_count=1,
                emojis=(GuildEmojiSnapshot(
                    emojiid=guildid + 1,
                    guildid=guildid,
                    name='runtime',
                    animated=False,
                    available=True,
                    observed_at=observed_at,
                ),),
                stickers=(GuildStickerSnapshot(
                    stickerid=guildid + 2,
                    guildid=guildid,
                    name='runtime',
                    description=None,
                    format_type=1,
                    sticker_type=2,
                    available=True,
                    observed_at=observed_at,
                ),),
                observed_at=observed_at,
            )
            self.assertTrue(await data.sync_guild(snapshot))

            inactive_snapshot = GuildSnapshot(
                guildid=snapshot.guildid,
                name=snapshot.name,
                icon_hash=snapshot.icon_hash,
                banner_hash=snapshot.banner_hash,
                description=snapshot.description,
                member_count=snapshot.member_count,
                emojis=snapshot.emojis,
                stickers=snapshot.stickers,
                observed_at=observed_at + timedelta(seconds=1),
            )
            self.assertTrue(await data.mark_guild_inactive(inactive_snapshot))

            async with connector.connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        SELECT
                          guild.active,
                          emoji.available AS emoji_available,
                          sticker.available AS sticker_available
                        FROM public.gostudy_guilds AS guild
                        JOIN public.gostudy_guild_emojis AS emoji
                          ON emoji.guildid = guild.guildid
                        JOIN public.gostudy_guild_stickers AS sticker
                          ON sticker.guildid = guild.guildid
                        WHERE guild.guildid = %s
                        """,
                        (guildid,),
                    )
                    row = await cursor.fetchone()
                    self.assertEqual(
                        (
                            row['active'],
                            row['emoji_available'],
                            row['sticker_available'],
                        ),
                        (False, False, False),
                    )

    async def asyncTearDown(self):
        if not self.admin.closed:
            async with self.admin.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = %s AND pid <> pg_backend_pid()
                    """,
                    (self.database_name,),
                )
                await cursor.execute(
                    sql.SQL('DROP DATABASE {}').format(
                        sql.Identifier(self.database_name)
                    )
                )
            await self.admin.close()

    async def test_version_20_upgrades_cleanly_and_reapply_rolls_back(self):
        conn = await psycopg.AsyncConnection.connect(
            self.database_url,
            autocommit=True,
        )
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(self.schema_v20)
                await cursor.execute(self.migration_v20_v21)
                await cursor.execute(
                    'SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1'
                )
                self.assertEqual((await cursor.fetchone())[0], 21)
                for table in (
                    'gostudy_guilds',
                    'gostudy_guild_emojis',
                    'gostudy_guild_stickers',
                ):
                    await cursor.execute('SELECT to_regclass(%s)', (f'public.{table}',))
                    self.assertIsNotNone((await cursor.fetchone())[0])

                with self.assertRaises(psycopg.Error):
                    await cursor.execute(self.migration_v20_v21)
                await conn.rollback()
                await cursor.execute(
                    'SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1'
                )
                self.assertEqual((await cursor.fetchone())[0], 21)
        finally:
            await conn.close()

    async def test_version_21_upgrades_to_22_with_exact_runtime_acl(self):
        conn = await psycopg.AsyncConnection.connect(
            self.database_url,
            autocommit=True,
        )
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(self.schema_v21)
                await cursor.execute(self.migration_v21_v22)
                await cursor.execute(
                    'SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1'
                )
                self.assertEqual((await cursor.fetchone())[0], 22)
                await self.assert_privilege_matrix(cursor)

                with self.assertRaises(psycopg.Error):
                    await cursor.execute(self.migration_v21_v22)
                await conn.rollback()
                await cursor.execute(
                    'SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1'
                )
                self.assertEqual((await cursor.fetchone())[0], 22)

            await self.exercise_runtime_as_lion()
        finally:
            await conn.close()

    async def test_upgrade_accepts_already_granted_runtime_acl(self):
        conn = await psycopg.AsyncConnection.connect(
            self.database_url,
            autocommit=True,
        )
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(self.schema_v21)
                await cursor.execute(
                    """
                    GRANT SELECT, INSERT, UPDATE ON TABLE
                      public.gostudy_guilds,
                      public.gostudy_guild_emojis,
                      public.gostudy_guild_stickers
                    TO lion
                    """
                )
                await cursor.execute(self.migration_v21_v22)
                await cursor.execute(
                    'SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1'
                )
                self.assertEqual((await cursor.fetchone())[0], 22)
                await self.assert_privilege_matrix(cursor)
        finally:
            await conn.close()

    async def test_fresh_schema_is_version_22_with_exact_runtime_acl(self):
        conn = await psycopg.AsyncConnection.connect(
            self.database_url,
            autocommit=True,
        )
        try:
            async with conn.cursor() as cursor:
                await cursor.execute(self.schema_v22)
                await cursor.execute(
                    'SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1'
                )
                self.assertEqual((await cursor.fetchone())[0], 22)
                await self.assert_privilege_matrix(cursor)
        finally:
            await conn.close()


if __name__ == '__main__':
    unittest.main()
