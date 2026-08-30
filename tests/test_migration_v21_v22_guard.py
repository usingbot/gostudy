from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'data/migration/v21-v22/migration.sql'
SCHEMA = ROOT / 'data/schema.sql'
TABLES = (
    'public.gostudy_guilds',
    'public.gostudy_guild_emojis',
    'public.gostudy_guild_stickers',
)


class MigrationV21V22GuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MIGRATION.read_text(encoding='utf-8')
        cls.schema = SCHEMA.read_text(encoding='utf-8')
        cls.normalized = re.sub(r'\s+', ' ', cls.source).strip()
        cls.marker = '-- Go Study Discord guild registry runtime privileges {{{'
        cls.block = cls.source[
            cls.source.index(cls.marker):
            cls.source.index('-- }}}', cls.source.index(cls.marker))
        ].strip()

    def test_only_version_21_is_accepted_before_acl_changes(self):
        guard_end = self.source.index('$$ LANGUAGE PLPGSQL;')
        acl_start = self.source.index(self.marker)
        self.assertIn('IF _current_version IS DISTINCT FROM 21 THEN', self.source)
        self.assertLess(guard_end, acl_start)
        self.assertIn("COALESCE(_current_version::TEXT, 'NULL')", self.source)

    def test_lock_transaction_and_version_record_match_safe_protocol(self):
        self.assertTrue(self.normalized.startswith(
            'BEGIN; LOCK TABLE public.VersionHistory '
            'IN SHARE ROW EXCLUSIVE MODE;'
        ))
        self.assertTrue(self.normalized.endswith(
            "INSERT INTO public.VersionHistory (version, author) "
            "VALUES (22, 'v21-v22 migration'); COMMIT;"
        ))

    def test_acl_is_narrow_and_covers_exactly_the_registry_tables(self):
        self.assertIn('GRANT SELECT, INSERT, UPDATE ON TABLE', self.block)
        self.assertIn('REVOKE DELETE, TRUNCATE ON TABLE', self.block)
        self.assertIn(
            'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE',
            self.block,
        )
        self.assertIn('FROM PUBLIC;', self.block)
        self.assertIn('FROM lion;', self.block)
        self.assertIn('TO lion;', self.block)
        for table in TABLES:
            self.assertEqual(self.block.count(table), 3)
        self.assertNotIn('GRANT ALL', self.block.upper())
        self.assertNotIn('TO PUBLIC', self.block.upper())
        self.assertNotIn('ALTER TABLE', self.block.upper())

    def test_fresh_schema_has_the_exact_v22_acl_block(self):
        schema_block = self.schema[
            self.schema.index(self.marker):
            self.schema.index('-- }}}', self.schema.index(self.marker))
        ].strip()
        self.assertEqual(schema_block, self.block)

    def test_application_and_fresh_schema_versions_are_22(self):
        constants = (ROOT / 'src/constants.py').read_text(encoding='utf-8')
        preflight = (ROOT / 'scripts/preflight.py').read_text(encoding='utf-8')
        self.assertIn('DATA_VERSION = 22', constants)
        self.assertIn('EXPECTED_SCHEMA_VERSION = 22', preflight)
        self.assertIn(
            "INSERT INTO VersionHistory (version, author) VALUES (22,",
            self.schema,
        )


if __name__ == '__main__':
    unittest.main()
