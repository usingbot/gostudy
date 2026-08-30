from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'data/migration/v20-v21/migration.sql'
SCHEMA = ROOT / 'data/schema.sql'
COG = ROOT / 'src/modules/gostudy_guild_registry/cog.py'
SERVICE = ROOT / 'src/modules/gostudy_guild_registry/service.py'


class MigrationV20V21GuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MIGRATION.read_text(encoding='utf-8')
        cls.schema = SCHEMA.read_text(encoding='utf-8')
        cls.normalized = re.sub(r'\s+', ' ', cls.source).strip()
        cls.guard_end = cls.source.index('$$ LANGUAGE PLPGSQL;')
        cls.table_start = cls.source.index('CREATE TABLE public.gostudy_guilds')
        cls.version_insert = cls.source.index(
            "VALUES (21, 'v20-v21 migration')"
        )

    def test_only_version_20_is_accepted_before_object_creation(self):
        self.assertIn('IF _current_version IS DISTINCT FROM 20 THEN', self.source)
        self.assertLess(self.guard_end, self.table_start)
        self.assertIn("COALESCE(_current_version::TEXT, 'NULL')", self.source)

    def test_lock_and_transaction_match_safe_protocol(self):
        self.assertTrue(self.normalized.startswith(
            'BEGIN; LOCK TABLE public.VersionHistory '
            'IN SHARE ROW EXCLUSIVE MODE;'
        ))
        self.assertTrue(self.normalized.endswith(
            "INSERT INTO public.VersionHistory (version, author) "
            "VALUES (21, 'v20-v21 migration'); COMMIT;"
        ))

    def test_migration_creates_exactly_three_registry_tables(self):
        body = self.source[self.guard_end:self.version_insert]
        self.assertEqual(
            re.findall(r'(?im)^CREATE\s+TABLE\s+([^\s(]+)', body),
            [
                'public.gostudy_guilds',
                'public.gostudy_guild_emojis',
                'public.gostudy_guild_stickers',
            ],
        )
        self.assertNotIn('IF NOT EXISTS', self.source.upper())

    def test_fresh_schema_and_migration_share_exact_v21_body(self):
        marker = '-- Go Study Discord guild registry {{{'
        migration_body = self.source[
            self.source.index(marker):
            self.source.index('-- }}}', self.source.index(marker))
        ].strip()
        schema_body = self.schema[
            self.schema.index(marker):
            self.schema.index('-- }}}', self.schema.index(marker))
        ].strip()
        self.assertEqual(schema_body, migration_body)

    def test_snowflakes_counts_and_text_are_constrained(self):
        for constraint in (
            'CHECK (guildid > 0)',
            'CHECK (emojiid > 0)',
            'CHECK (stickerid > 0)',
            'CHECK (member_count IS NULL OR member_count >= 0)',
            'CHECK (char_length(name) BETWEEN 1 AND 100)',
        ):
            self.assertIn(constraint, self.source)
        self.assertIn('description VARCHAR(120)', self.source)
        self.assertIn('description VARCHAR(1000)', self.source)

    def test_assets_have_deliberate_guild_ownership_and_cascade(self):
        self.assertEqual(
            self.source.count(
                'REFERENCES public.gostudy_guilds (guildid) ON DELETE CASCADE'
            ),
            2,
        )
        self.assertNotIn('guild_config', self.source)
        self.assertNotIn('gostudy_web', self.source)

    def test_schema_has_no_url_invite_or_user_membership_columns(self):
        registry_body = self.source[
            self.table_start:self.source.index('-- }}}', self.table_start)
        ]
        for forbidden in (
            'icon_url',
            'banner_url',
            'cdn_url',
            'invite',
            'userid',
            'memberid',
            'message',
            'channelid',
            'roleid',
        ):
            self.assertNotIn(forbidden, registry_body.lower())

    def test_discord_events_cover_startup_and_asset_changes(self):
        source = COG.read_text(encoding='utf-8')
        for event in (
            'on_ready',
            'on_guild_join',
            'on_guild_remove',
            'on_guild_update',
            'on_guild_emojis_update',
            'on_guild_stickers_update',
        ):
            self.assertIn(f"@LionCog.listener('{event}')", source)

    def test_service_uses_asset_keys_and_never_member_lists_or_urls(self):
        source = SERVICE.read_text(encoding='utf-8')
        self.assertIn("getattr(asset, 'key', None)", source)
        self.assertNotIn("getattr(guild, 'members'", source)
        self.assertNotIn('.url', source)
        self.assertNotIn('.read(', source)

    def test_application_schema_version_is_21(self):
        constants = (ROOT / 'src/constants.py').read_text(encoding='utf-8')
        preflight = (ROOT / 'scripts/preflight.py').read_text(encoding='utf-8')
        self.assertIn('DATA_VERSION = 21', constants)
        self.assertIn('EXPECTED_SCHEMA_VERSION = 21', preflight)
        self.assertIn(
            "INSERT INTO VersionHistory (version, author) VALUES (21,",
            self.schema,
        )


if __name__ == '__main__':
    unittest.main()
