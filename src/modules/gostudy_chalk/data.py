from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias

from psycopg import AsyncCursor
from psycopg.rows import dict_row

from data import Registry


ChalkTransactionType: TypeAlias = Literal[
    'admin_grant',
    'admin_deduct',
    'study_earning',
    'shop_purchase',
    'refund',
    'migration_adjustment',
    'system_adjustment',
]


@dataclass(frozen=True, slots=True)
class ChalkAccount:
    userid: int
    balance: int
    lifetime_credited: int
    lifetime_debited: int
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChalkTransaction:
    transactionid: int
    userid: int
    amount: int
    balance_after: int
    transaction_type: ChalkTransactionType
    idempotency_key: str
    actor_userid: int | None
    reference_type: str | None
    reference_id: str | None
    reversal_of_transactionid: int | None
    reason: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ChalkTransactionResult:
    transaction: ChalkTransaction
    account: ChalkAccount
    replayed: bool


def chalk_cursor(conn):
    """Use psycopg directly so financial database errors always propagate."""
    return AsyncCursor(conn, row_factory=dict_row)


def _account_from_row(row) -> ChalkAccount:
    return ChalkAccount(
        userid=row['userid'],
        balance=row['balance'],
        lifetime_credited=row['lifetime_credited'],
        lifetime_debited=row['lifetime_debited'],
        created_at=row['created_at'],
        updated_at=row['updated_at'],
    )


def _transaction_from_row(row) -> ChalkTransaction:
    return ChalkTransaction(
        transactionid=row['transactionid'],
        userid=row['userid'],
        amount=row['amount'],
        balance_after=row['balance_after'],
        transaction_type=row['transaction_type'],
        idempotency_key=row['idempotency_key'],
        actor_userid=row['actor_userid'],
        reference_type=row['reference_type'],
        reference_id=row['reference_id'],
        reversal_of_transactionid=row['reversal_of_transactionid'],
        reason=row['reason'],
        created_at=row['created_at'],
    )


class GoStudyChalkData(Registry, name='gostudy_chalk'):
    async def fetch_account(self, userid: int) -> ChalkAccount:
        """Return the global Chalk account, or a non-persistent zero account."""
        if userid <= 0:
            raise ValueError('Chalk userid must be positive.')

        query = """
            SELECT
              userid,
              balance,
              lifetime_credited,
              lifetime_debited,
              created_at,
              updated_at
            FROM public.gostudy_chalk_accounts
            WHERE userid = %s
        """
        async with self._conn.connection() as conn:
            async with chalk_cursor(conn) as cursor:
                await cursor.execute(query, (userid,))
                row = await cursor.fetchone()

        if row is None:
            return ChalkAccount(
                userid=userid,
                balance=0,
                lifetime_credited=0,
                lifetime_debited=0,
                created_at=None,
                updated_at=None,
            )
        return _account_from_row(row)

    async def fetch_balance(self, userid: int) -> int:
        """Return a user's exact integer Chalk balance."""
        account = await self.fetch_account(userid)
        return account.balance

    async def fetch_transactions(
        self,
        userid: int,
        before_transactionid: int | None = None,
        limit: int = 100,
    ) -> list[ChalkTransaction]:
        """Return one newest-first keyset page of immutable ledger rows."""
        if userid <= 0:
            raise ValueError('Chalk userid must be positive.')
        if before_transactionid is not None and before_transactionid <= 0:
            raise ValueError('Chalk transaction cursor must be positive.')
        limit = max(1, min(limit, 1000))

        query = """
            SELECT
              transactionid,
              userid,
              amount,
              balance_after,
              transaction_type,
              idempotency_key,
              actor_userid,
              reference_type,
              reference_id,
              reversal_of_transactionid,
              reason,
              created_at
            FROM public.gostudy_chalk_transactions
            WHERE
              userid = %s
              AND (%s::BIGINT IS NULL OR transactionid < %s::BIGINT)
            ORDER BY transactionid DESC
            LIMIT %s
        """
        async with self._conn.connection() as conn:
            async with chalk_cursor(conn) as cursor:
                await cursor.execute(
                    query,
                    (userid, before_transactionid, before_transactionid, limit),
                )
                rows = await cursor.fetchall()
        return [_transaction_from_row(row) for row in rows]

    async def apply_transaction(
        self,
        userid: int,
        amount: int,
        transaction_type: ChalkTransactionType,
        idempotency_key: str,
        *,
        actor_userid: int | None = None,
        reference_type: str | None = None,
        reference_id: str | None = None,
        reversal_of_transactionid: int | None = None,
        reason: str | None = None,
    ) -> ChalkTransactionResult:
        """Apply or replay one transaction through the authoritative function."""
        query = """
            SELECT *
            FROM public.gostudy_apply_chalk_transaction(
              %s::BIGINT,
              %s::BIGINT,
              %s::TEXT,
              %s::TEXT,
              %s::BIGINT,
              %s::TEXT,
              %s::TEXT,
              %s::BIGINT,
              %s::TEXT
            )
        """
        params = (
            userid,
            amount,
            transaction_type,
            idempotency_key,
            actor_userid,
            reference_type,
            reference_id,
            reversal_of_transactionid,
            reason,
        )
        async with self._conn.connection() as conn:
            async with conn.transaction():
                async with chalk_cursor(conn) as cursor:
                    await cursor.execute(query, params)
                    row = await cursor.fetchone()

        if row is None:
            raise RuntimeError('Chalk transaction function returned no result.')

        account = ChalkAccount(
            userid=row['userid'],
            balance=row['account_balance'],
            lifetime_credited=row['account_lifetime_credited'],
            lifetime_debited=row['account_lifetime_debited'],
            created_at=row['account_created_at'],
            updated_at=row['account_updated_at'],
        )
        return ChalkTransactionResult(
            transaction=_transaction_from_row(row),
            account=account,
            replayed=row['replayed'],
        )
