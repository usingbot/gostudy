from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'data/migration/v16-v17/migration.sql'


class MigrationV16V17GuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MIGRATION.read_text(encoding='utf-8')
        cls.normalized = re.sub(r'\s+', ' ', cls.source).strip()
        cls.guard_end = cls.source.index('$$ LANGUAGE PLPGSQL;')
        cls.first_v17_object = cls.source.index(
            'CREATE TABLE public.gostudy_reward_notifications'
        )
        cls.version_17_insert = cls.source.index(
            "VALUES (17, 'v16-v17 migration')"
        )
        required_match = re.search(
            r'IF _current_version IS DISTINCT FROM (\d+) THEN',
            cls.source,
        )
        if required_match is None:
            raise AssertionError('Migration version guard is missing.')
        cls.required_version = int(required_match.group(1))

    def guard_rejects(self, current_version):
        return (
            current_version is None
            or current_version != self.required_version
        )

    def test_version_16_is_the_only_accepted_version(self):
        self.assertIn(
            'IF _current_version IS DISTINCT FROM 16 THEN',
            self.source,
        )
        self.assertFalse(self.guard_rejects(16))

    def test_version_15_and_null_reject_before_creating_v17_objects(self):
        self.assertTrue(self.guard_rejects(15))
        self.assertTrue(self.guard_rejects(None))
        self.assertIn("COALESCE(_current_version::TEXT, 'NULL')", self.source)
        self.assertLess(self.guard_end, self.first_v17_object)

    def test_version_17_reapplication_is_rejected(self):
        self.assertTrue(self.guard_rejects(17))
        self.assertNotIn('IF NOT EXISTS', self.source.upper())

    def test_guard_lock_and_failure_preserve_version_history(self):
        self.assertTrue(self.normalized.startswith(
            'BEGIN; LOCK TABLE VersionHistory IN SHARE ROW EXCLUSIVE MODE;'
        ))
        self.assertIn(
            '_current_version := ( SELECT version FROM VersionHistory '
            'ORDER BY time DESC LIMIT 1 );',
            self.normalized,
        )
        self.assertIn('RAISE EXCEPTION', self.source[:self.guard_end])
        self.assertLess(self.guard_end, self.version_17_insert)
        self.assertTrue(self.normalized.endswith(
            "VALUES (17, 'v16-v17 migration'); COMMIT;"
        ))


if __name__ == '__main__':
    unittest.main()
