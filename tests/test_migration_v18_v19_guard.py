from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'data/migration/v18-v19/migration.sql'
SCHEMA = ROOT / 'data/schema.sql'


class MigrationV18V19GuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MIGRATION.read_text(encoding='utf-8')
        cls.schema = SCHEMA.read_text(encoding='utf-8')
        cls.normalized = re.sub(r'\s+', ' ', cls.source).strip()
        cls.guard_end = cls.source.index('$$ LANGUAGE PLPGSQL;')
        cls.first_v19_object = cls.source.index(
            'CREATE FUNCTION public.gostudy_admin_grant_chalk'
        )
        cls.version_insert = cls.source.index(
            "VALUES (19, 'v18-v19 migration')"
        )

    def test_only_version_18_is_accepted(self):
        self.assertIn(
            'IF _current_version IS DISTINCT FROM 18 THEN',
            self.source,
        )
        self.assertIn("COALESCE(_current_version::TEXT, 'NULL')", self.source)

    def test_guard_precedes_every_v19_object(self):
        self.assertLess(self.guard_end, self.first_v19_object)
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
            "VALUES (19, 'v18-v19 migration'); COMMIT;"
        ))

    def test_migration_is_intentionally_non_idempotent(self):
        self.assertNotIn('IF NOT EXISTS', self.source.upper())
        self.assertNotIn('CREATE OR REPLACE', self.source.upper())

    def test_migration_creates_functions_only_and_no_economic_rows(self):
        body = self.source[self.guard_end:self.version_insert]
        self.assertNotRegex(body, r'(?i)\bCREATE\s+(?:TABLE|INDEX|SEQUENCE|TYPE)\b')
        self.assertNotIn('INSERT INTO public.gostudy_chalk_accounts', body)
        self.assertNotIn('INSERT INTO public.gostudy_chalk_transactions', body)
        self.assertNotIn('UPDATE public.gostudy_chalk_accounts', body)

    def test_fresh_schema_and_migration_share_exact_v19_body(self):
        marker = '-- Go Study Chalk admin API {{{'
        migration_start = self.source.index(marker)
        migration_body = self.source[
            migration_start:self.source.index('-- }}}', migration_start)
        ].strip()
        schema_start = self.schema.index(marker)
        schema_body = self.schema[
            schema_start:self.schema.index('-- }}}', schema_start)
        ].strip()
        self.assertEqual(schema_body, migration_body)

    def test_exact_wrapper_signatures_and_result_contract(self):
        for signature in (
            'public.gostudy_admin_grant_chalk(',
            'public.gostudy_admin_deduct_chalk(',
            'public.gostudy_admin_get_chalk_account(',
            'public.gostudy_admin_list_chalk_transactions(',
        ):
            self.assertIn(signature, self.source)

        result_fields = (
            'transactionid BIGINT',
            'userid BIGINT',
            'amount BIGINT',
            'balance_after BIGINT',
            'transaction_type TEXT',
            'idempotency_key VARCHAR(128)',
            'account_balance BIGINT',
            'account_lifetime_credited BIGINT',
            'account_lifetime_debited BIGINT',
            'replayed BOOLEAN',
        )
        grant_start = self.source.index(
            'CREATE FUNCTION public.gostudy_admin_grant_chalk'
        )
        deduct_start = self.source.index(
            'CREATE FUNCTION public.gostudy_admin_deduct_chalk'
        )
        read_start = self.source.index(
            'CREATE FUNCTION public.gostudy_admin_get_chalk_account'
        )
        mutation_source = self.source[grant_start:read_start]
        for field in result_fields:
            self.assertEqual(
                len(re.findall(rf'(?m)^  {re.escape(field)},?$', mutation_source)),
                2,
            )

    def test_mutation_wrappers_are_narrow_and_delegate(self):
        self.assertEqual(
            self.source.count('FROM public.gostudy_apply_chalk_transaction('),
            2,
        )
        self.assertIn("'admin_grant'::TEXT", self.source)
        self.assertIn("'admin_deduct'::TEXT", self.source)
        self.assertIn('_negative_amount := 0::NUMERIC - _amount::NUMERIC;', self.source)
        self.assertNotRegex(
            self.source,
            r'CREATE FUNCTION public\.gostudy_admin_(?:grant|deduct)_chalk\([\s\S]*?_transaction_type',
        )

    def test_validation_and_history_bounds_are_explicit(self):
        self.assertIn('_amount < 1 OR _amount > 1000000', self.source)
        self.assertIn("_reason IS DISTINCT FROM btrim(_reason)", self.source)
        self.assertIn("'^admin:' || _actor_userid::TEXT", self.source)
        self.assertIn('[89ab][0-9a-f]{3}', self.source)
        self.assertIn('_before_transactionid <= 0', self.source)
        self.assertIn('_limit < 1 OR _limit > 50', self.source)
        self.assertIn('ORDER BY transactions.transactionid DESC', self.source)
        self.assertIn('transactions.transactionid < _before_transactionid', self.source)

    def test_all_functions_are_hardened_and_private(self):
        self.assertEqual(self.source.count('SECURITY DEFINER'), 4)
        self.assertEqual(self.source.count('SET search_path = pg_catalog'), 4)
        self.assertEqual(self.source.count('REVOKE ALL ON FUNCTION'), 4)
        self.assertNotRegex(self.source, r'(?m)^GRANT\s')
        self.assertNotIn(
            'REVOKE ALL ON FUNCTION public.gostudy_apply_chalk_transaction',
            self.source,
        )

    def test_version_is_recorded_after_all_revocations(self):
        self.assertLess(self.source.rindex('REVOKE ALL ON FUNCTION'), self.version_insert)


if __name__ == '__main__':
    unittest.main()
