from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'data/migration/v17-v18/migration.sql'


class MigrationV17V18GuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MIGRATION.read_text(encoding='utf-8')
        cls.normalized = re.sub(r'\s+', ' ', cls.source).strip()
        cls.guard_end = cls.source.index('$$ LANGUAGE PLPGSQL;')
        cls.first_v18_object = cls.source.index(
            'CREATE TABLE public.gostudy_chalk_accounts'
        )
        cls.version_insert = cls.source.index(
            "VALUES (18, 'v17-v18 migration')"
        )

    def test_only_version_17_is_accepted(self):
        self.assertIn(
            'IF _current_version IS DISTINCT FROM 17 THEN',
            self.source,
        )
        self.assertIn("COALESCE(_current_version::TEXT, 'NULL')", self.source)

    def test_guard_precedes_every_v18_object(self):
        self.assertLess(self.guard_end, self.first_v18_object)
        self.assertIn('RAISE EXCEPTION', self.source[:self.guard_end])

    def test_lock_and_transaction_match_safe_protocol(self):
        self.assertTrue(self.normalized.startswith(
            'BEGIN; LOCK TABLE VersionHistory IN SHARE ROW EXCLUSIVE MODE;'
        ))
        self.assertIn(
            '_current_version := ( SELECT version FROM VersionHistory '
            'ORDER BY time DESC LIMIT 1 );',
            self.normalized,
        )
        self.assertTrue(self.normalized.endswith(
            "VALUES (18, 'v17-v18 migration'); COMMIT;"
        ))

    def test_migration_is_intentionally_non_idempotent(self):
        self.assertNotIn('IF NOT EXISTS', self.source.upper())
        self.assertNotIn('CREATE OR REPLACE', self.source.upper())

    def test_version_is_recorded_after_all_objects_and_revocations(self):
        self.assertLess(
            self.source.index('REVOKE ALL ON FUNCTION'),
            self.version_insert,
        )
        self.assertLess(
            self.source.index('REVOKE ALL ON TABLE'),
            self.version_insert,
        )

    def test_no_historical_chalk_is_seeded(self):
        pre_function = self.source[
            self.first_v18_object:
            self.source.index(
                'CREATE FUNCTION public.gostudy_apply_chalk_transaction'
            )
        ]
        self.assertNotIn(
            'INSERT INTO public.gostudy_chalk_accounts',
            pre_function,
        )
        self.assertNotIn(
            'INSERT INTO public.gostudy_chalk_transactions',
            pre_function,
        )


if __name__ == '__main__':
    unittest.main()
