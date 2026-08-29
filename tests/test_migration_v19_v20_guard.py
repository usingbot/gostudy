from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'data/migration/v19-v20/migration.sql'
SCHEMA = ROOT / 'data/schema.sql'


class MigrationV19V20GuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MIGRATION.read_text(encoding='utf-8')
        cls.schema = SCHEMA.read_text(encoding='utf-8')
        cls.normalized = re.sub(r'\s+', ' ', cls.source).strip()
        cls.guard_end = cls.source.index('$$ LANGUAGE PLPGSQL;')
        cls.function_start = cls.source.index(
            'CREATE FUNCTION public.gostudy_purchase_board_item_chalk'
        )
        cls.version_insert = cls.source.index(
            "VALUES (20, 'v19-v20 migration')"
        )
        cls.function_source = cls.source[
            cls.function_start:cls.source.index('-- }}}', cls.function_start)
        ]

    def test_only_version_19_is_accepted_before_object_creation(self):
        self.assertIn(
            'IF _current_version IS DISTINCT FROM 19 THEN',
            self.source,
        )
        self.assertIn("COALESCE(_current_version::TEXT, 'NULL')", self.source)
        self.assertLess(self.guard_end, self.function_start)
        self.assertIn('RAISE EXCEPTION', self.source[:self.guard_end])

    def test_lock_and_transaction_match_safe_protocol(self):
        self.assertTrue(self.normalized.startswith(
            'BEGIN; LOCK TABLE public.VersionHistory '
            'IN SHARE ROW EXCLUSIVE MODE;'
        ))
        self.assertIn(
            '_current_version := ( SELECT version FROM public.VersionHistory '
            'ORDER BY time DESC LIMIT 1 );',
            self.normalized,
        )
        self.assertTrue(self.normalized.endswith(
            "INSERT INTO public.VersionHistory (version, author) "
            "VALUES (20, 'v19-v20 migration'); COMMIT;"
        ))

    def test_migration_creates_only_the_narrow_function(self):
        body = self.source[self.guard_end:self.version_insert]
        self.assertEqual(
            re.findall(r'(?im)^CREATE\s+FUNCTION\s+([^\s(]+)', body),
            ['public.gostudy_purchase_board_item_chalk'],
        )
        self.assertNotRegex(
            body,
            r'(?i)\bCREATE\s+(?:TABLE|INDEX|SEQUENCE|TYPE|TRIGGER)\b',
        )
        self.assertNotRegex(
            body,
            r'(?i)\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+'
            r'public\.gostudy_chalk_(?:accounts|transactions)\b',
        )
        self.assertNotIn('IF NOT EXISTS', self.source.upper())
        self.assertNotIn('CREATE OR REPLACE', self.source.upper())

    def test_fresh_schema_and_migration_share_exact_v20_body(self):
        marker = '-- Go Study Chalk board purchase API {{{'
        migration_start = self.source.index(marker)
        migration_body = self.source[
            migration_start:self.source.index('-- }}}', migration_start)
        ].strip()
        schema_start = self.schema.index(marker)
        schema_body = self.schema[
            schema_start:self.schema.index('-- }}}', schema_start)
        ].strip()
        self.assertEqual(schema_body, migration_body)

    def test_result_contract_is_complete_and_canonical(self):
        for field in (
            'transactionid BIGINT',
            'userid BIGINT',
            'amount BIGINT',
            'balance_after BIGINT',
            'transaction_type TEXT',
            'idempotency_key VARCHAR(128)',
            'actor_userid BIGINT',
            'reference_type VARCHAR(64)',
            'reference_id VARCHAR(128)',
            'reversal_of_transactionid BIGINT',
            'reason VARCHAR(500)',
            'created_at TIMESTAMPTZ',
            'account_balance BIGINT',
            'account_lifetime_credited BIGINT',
            'account_lifetime_debited BIGINT',
            'account_created_at TIMESTAMPTZ',
            'account_updated_at TIMESTAMPTZ',
            'replayed BOOLEAN',
        ):
            self.assertRegex(
                self.function_source,
                rf'(?m)^  {re.escape(field)},?$',
            )

    def test_wrapper_validation_is_canonical_and_bounded(self):
        self.assertIn('_userid IS NULL OR _userid <= 0', self.function_source)
        self.assertIn(
            '_amount IS NULL OR _amount < 1 OR _amount > 1000000',
            self.function_source,
        )
        self.assertIn("'^shop:' || _userid::TEXT", self.function_source)
        self.assertGreaterEqual(
            self.function_source.count(
                '[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-'
                '[89ab][0-9a-f]{3}-[0-9a-f]{12}'
            ),
            2,
        )

    def test_wrapper_hardcodes_payload_and_only_delegates(self):
        self.assertEqual(
            self.function_source.count(
                'FROM public.gostudy_apply_chalk_transaction('
            ),
            1,
        )
        self.assertEqual(
            re.findall(
                r'(?i)\bFROM\s+public\.([a-z0-9_]+)',
                self.function_source,
            ),
            ['gostudy_apply_chalk_transaction'],
        )
        self.assertIn(
            '_negative_amount := 0::NUMERIC - _amount::NUMERIC;',
            self.function_source,
        )
        for hardcoded in (
            "'shop_purchase'::TEXT",
            'NULL::BIGINT',
            "'board_purchase'::TEXT",
            'NULL::TEXT',
        ):
            self.assertIn(hardcoded, self.function_source)
        self.assertNotIn('_transaction_type', self.function_source)
        self.assertNotIn('_actor_userid', self.function_source)
        self.assertNotIn('_reversal', self.function_source)
        self.assertNotIn('_reason', self.function_source)

    def test_function_is_hardened_private_and_ungranted(self):
        self.assertIn(
            'LANGUAGE PLPGSQL\nSECURITY DEFINER\nSET search_path = pg_catalog',
            self.function_source,
        )
        self.assertIn(
            'REVOKE ALL ON FUNCTION public.gostudy_purchase_board_item_chalk(\n'
            '  BIGINT, BIGINT, TEXT, TEXT\n) FROM PUBLIC;',
            self.function_source,
        )
        self.assertNotRegex(self.source, r'(?m)^GRANT\s')
        self.assertNotRegex(self.source, r'(?im)^ALTER\s+FUNCTION\s')

    def test_version_is_recorded_after_revocation(self):
        self.assertLess(self.source.rindex('REVOKE ALL ON FUNCTION'), self.version_insert)


if __name__ == '__main__':
    unittest.main()
