from dataclasses import FrozenInstanceError
from pathlib import Path
import inspect
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from modules.gostudy_chalk.data import (  # noqa: E402
    ChalkAccount,
    ChalkTransaction,
    ChalkTransactionResult,
    GoStudyChalkData,
)


MIGRATION = ROOT / 'data/migration/v17-v18/migration.sql'
SCHEMA = ROOT / 'data/schema.sql'
DATA_MODULE = ROOT / 'src/modules/gostudy_chalk/data.py'


class ChalkSchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.migration = MIGRATION.read_text(encoding='utf-8')
        cls.schema = SCHEMA.read_text(encoding='utf-8')
        cls.normalized = re.sub(r'\s+', ' ', cls.migration)

    def test_fresh_schema_and_migration_share_exact_chalk_body(self):
        marker = '-- Chalk accounts are created lazily'
        migration_body = self.migration[
            self.migration.index(marker):
            self.migration.index('INSERT INTO VersionHistory (version, author)')
        ].strip()
        schema_start = self.schema.index(marker)
        schema_body = self.schema[
            schema_start:
            self.schema.index('-- }}}', schema_start)
        ].strip()
        self.assertEqual(schema_body, migration_body)

    def test_account_invariant_uses_numeric_intermediates(self):
        self.assertIn(
            'balance::NUMERIC =\n'
            '        lifetime_credited::NUMERIC - lifetime_debited::NUMERIC',
            self.migration,
        )

    def test_transaction_types_and_global_idempotency_are_constrained(self):
        for transaction_type in (
            'admin_grant',
            'admin_deduct',
            'study_earning',
            'shop_purchase',
            'refund',
            'migration_adjustment',
            'system_adjustment',
        ):
            self.assertIn(f"'{transaction_type}'", self.migration)
        self.assertIn('UNIQUE (idempotency_key)', self.migration)
        self.assertNotIn('UNIQUE (userid, idempotency_key)', self.migration)

    def test_authoritative_function_is_hardened(self):
        self.assertIn('SECURITY DEFINER\nSET search_path = pg_catalog', self.migration)
        self.assertIn('pg_catalog.pg_advisory_xact_lock', self.migration)
        self.assertIn('pg_catalog.hashtextextended(_idempotency_key, 0)', self.migration)
        self.assertIn('FOR UPDATE;', self.migration)
        self.assertIn('FROM PUBLIC;', self.migration)
        self.assertNotRegex(self.migration, r'(?m)^GRANT\s')

    def test_every_economic_field_participates_in_replay_comparison(self):
        for field in (
            'userid',
            'amount',
            'transaction_type',
            'actor_userid',
            'reference_type',
            'reference_id',
            'reversal_of_transactionid',
            'reason',
        ):
            self.assertIn(
                f'_existing.{field} IS DISTINCT FROM _{field}',
                self.migration,
            )

    def test_all_economic_arithmetic_is_numeric(self):
        self.assertIn('_amount_numeric := _amount::NUMERIC', self.migration)
        self.assertIn('_account.balance::NUMERIC + _amount_numeric', self.migration)
        self.assertIn('0::NUMERIC - _amount_numeric', self.migration)
        self.assertIn("'-9223372036854775808'::NUMERIC", self.migration)
        self.assertIn("'9223372036854775807'::NUMERIC", self.migration)
        self.assertNotIn('lifetime_debited + (-_amount)', self.migration)

    def test_refunds_lock_and_sum_the_referenced_purchase(self):
        refund_section = self.migration[
            self.migration.index('IF _reversal_of_transactionid IS NOT NULL THEN'):
            self.migration.index('_amount_numeric := _amount::NUMERIC')
        ]
        self.assertIn('FOR UPDATE;', refund_section)
        self.assertIn("_reversed.transaction_type IS DISTINCT FROM 'shop_purchase'", refund_section)
        self.assertIn('sum(refunds.amount::NUMERIC)', refund_section)
        self.assertIn('_existing_refunds + _amount::NUMERIC > _purchase_debit', refund_section)

    def test_ledger_has_update_delete_and_truncate_guards(self):
        self.assertIn('BEFORE UPDATE OR DELETE', self.migration)
        self.assertIn('BEFORE TRUNCATE', self.migration)
        self.assertIn('create a compensating/reversal transaction instead', self.migration)

    def test_fresh_schema_declares_version_18(self):
        self.assertIn(
            "INSERT INTO VersionHistory (version, author) VALUES (18,",
            self.schema,
        )


class ChalkDataContractTests(unittest.TestCase):
    def test_models_are_frozen(self):
        account = ChalkAccount(1, 0, 0, 0, None, None)
        with self.assertRaises(FrozenInstanceError):
            account.balance = 1

        transaction = ChalkTransaction(
            1, 1, 1, 1, 'system_adjustment', 'key', None,
            None, None, None, 'reason', None,
        )
        result = ChalkTransactionResult(transaction, account, False)
        with self.assertRaises(FrozenInstanceError):
            result.replayed = True

    def test_data_registry_exposes_only_the_controlled_public_api(self):
        public_coroutines = {
            name
            for name, value in GoStudyChalkData.__dict__.items()
            if not name.startswith('_') and inspect.iscoroutinefunction(value)
        }
        self.assertEqual(
            public_coroutines,
            {
                'fetch_account',
                'fetch_balance',
                'fetch_transactions',
                'apply_transaction',
            },
        )

    def test_mutation_uses_direct_psycopg_cursor(self):
        source = DATA_MODULE.read_text(encoding='utf-8')
        self.assertIn('from psycopg import AsyncCursor', source)
        self.assertIn('async with chalk_cursor(conn) as cursor:', source)
        self.assertNotIn('AsyncLoggingCursor', source)

    def test_module_does_not_couple_to_legacy_economy(self):
        module_source = ''.join(
            path.read_text(encoding='utf-8')
            for path in (ROOT / 'src/modules/gostudy_chalk').glob('*.py')
        )
        self.assertNotIn('modules.economy', module_source)
        self.assertNotIn('modules.shop', module_source)
        self.assertNotIn('MAX_COINS', module_source)


if __name__ == '__main__':
    unittest.main()
