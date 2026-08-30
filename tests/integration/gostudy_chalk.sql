-- Run only with psql against an explicitly authorized disposable schema v21
-- database. Every write in this sequential suite is rolled back.
BEGIN;

-- A missing account is zero by API contract and is not created by a read.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM public.gostudy_chalk_accounts WHERE userid = 918000000000001
  ) THEN
    RAISE EXCEPTION 'missing-account fixture unexpectedly exists';
  END IF;
END;
$$;

-- +10 then -3, including exact cached totals and ledger snapshots.
SELECT * FROM public.gostudy_apply_chalk_transaction(
  918000000000001,
  10,
  'admin_grant',
  'chalk-sql:grant-10',
  918000000009999,
  NULL,
  NULL,
  NULL,
  'Sequential grant test'
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM public.gostudy_chalk_accounts
    WHERE userid = 918000000000001
      AND balance = 10
      AND lifetime_credited = 10
      AND lifetime_debited = 0
  ) THEN
    RAISE EXCEPTION 'grant account assertion failed';
  END IF;
END;
$$;

SELECT * FROM public.gostudy_apply_chalk_transaction(
  918000000000001,
  -3,
  'admin_deduct',
  'chalk-sql:deduct-3',
  918000000009999,
  NULL,
  NULL,
  NULL,
  'Sequential deduction test'
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM public.gostudy_chalk_accounts
    WHERE userid = 918000000000001
      AND balance = 7
      AND lifetime_credited = 10
      AND lifetime_debited = 3
      AND balance::NUMERIC =
        lifetime_credited::NUMERIC - lifetime_debited::NUMERIC
  ) OR NOT EXISTS (
    SELECT 1
    FROM public.gostudy_chalk_transactions
    WHERE idempotency_key = 'chalk-sql:deduct-3'
      AND balance_after = 7
  ) THEN
    RAISE EXCEPTION 'deduction or balance-after assertion failed';
  END IF;
END;
$$;

-- Overspend fails without changing either account or ledger.
DO $$
DECLARE
  _before_count BIGINT;
BEGIN
  SELECT count(*) INTO _before_count
  FROM public.gostudy_chalk_transactions
  WHERE userid = 918000000000001;

  BEGIN
    PERFORM public.gostudy_apply_chalk_transaction(
      918000000000001,
      -8,
      'admin_deduct',
      'chalk-sql:overspend',
      918000000009999,
      NULL,
      NULL,
      NULL,
      'Overspend rejection test'
    );
    RAISE EXCEPTION 'overspend unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '23514' THEN
    NULL;
  END;

  IF (SELECT balance FROM public.gostudy_chalk_accounts
      WHERE userid = 918000000000001) <> 7
     OR (SELECT count(*) FROM public.gostudy_chalk_transactions
         WHERE userid = 918000000000001) <> _before_count THEN
    RAISE EXCEPTION 'overspend was not atomic';
  END IF;
END;
$$;

-- Exact replay applies once and reports replayed=true.
DO $$
DECLARE
  _replayed BOOLEAN;
BEGIN
  SELECT result.replayed INTO _replayed
  FROM public.gostudy_apply_chalk_transaction(
    918000000000001,
    10,
    'admin_grant',
    'chalk-sql:grant-10',
    918000000009999,
    NULL,
    NULL,
    NULL,
    'Sequential grant test'
  ) AS result;

  IF _replayed IS NOT TRUE
     OR (SELECT count(*) FROM public.gostudy_chalk_transactions
         WHERE idempotency_key = 'chalk-sql:grant-10') <> 1
     OR (SELECT balance FROM public.gostudy_chalk_accounts
         WHERE userid = 918000000000001) <> 7 THEN
    RAISE EXCEPTION 'exact replay assertion failed';
  END IF;
END;
$$;

-- Changed economic payloads conflict explicitly.
DO $$
BEGIN
  BEGIN
    PERFORM public.gostudy_apply_chalk_transaction(
      918000000000002,
      10,
      'admin_grant',
      'chalk-sql:grant-10',
      918000000009999,
      NULL,
      NULL,
      NULL,
      'Sequential grant test'
    );
    RAISE EXCEPTION 'changed-user replay unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22000' THEN
    NULL;
  END;

  BEGIN
    PERFORM public.gostudy_apply_chalk_transaction(
      918000000000001,
      11,
      'admin_grant',
      'chalk-sql:grant-10',
      918000000009999,
      NULL,
      NULL,
      NULL,
      'Sequential grant test'
    );
    RAISE EXCEPTION 'changed-amount replay unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22000' THEN
    NULL;
  END;
END;
$$;

-- Validation failures.
DO $$
BEGIN
  BEGIN
    PERFORM public.gostudy_apply_chalk_transaction(
      918000000000001, 0, 'system_adjustment', 'chalk-sql:zero',
      NULL, NULL, NULL, NULL, 'Zero rejection test'
    );
    RAISE EXCEPTION 'zero amount unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_apply_chalk_transaction(
      918000000000001, 1, 'invalid_type', 'chalk-sql:invalid-type',
      NULL, NULL, NULL, NULL, NULL
    );
    RAISE EXCEPTION 'invalid type unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_apply_chalk_transaction(
      918000000000001, 1, 'admin_grant', 'chalk-sql:no-actor',
      NULL, NULL, NULL, NULL, 'Missing actor test'
    );
    RAISE EXCEPTION 'missing admin actor unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_apply_chalk_transaction(
      918000000000001, 1, 'admin_grant', 'chalk-sql:no-reason',
      918000000009999, NULL, NULL, NULL, NULL
    );
    RAISE EXCEPTION 'missing admin reason unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;
END;
$$;

-- Ledger UPDATE, DELETE, and TRUNCATE are rejected defensively.
DO $$
BEGIN
  BEGIN
    UPDATE public.gostudy_chalk_transactions
    SET reason = 'forbidden'
    WHERE idempotency_key = 'chalk-sql:grant-10';
    RAISE EXCEPTION 'ledger update unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
  END;

  BEGIN
    DELETE FROM public.gostudy_chalk_transactions
    WHERE idempotency_key = 'chalk-sql:grant-10';
    RAISE EXCEPTION 'ledger delete unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
  END;

  BEGIN
    TRUNCATE TABLE public.gostudy_chalk_transactions;
    RAISE EXCEPTION 'ledger truncate unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
  END;
END;
$$;

-- Values beyond 32-bit remain exact. Positive and debit overflows are atomic.
SELECT * FROM public.gostudy_apply_chalk_transaction(
  918000000000003,
  9223372036854775807,
  'system_adjustment',
  'chalk-sql:bigint-max',
  NULL,
  NULL,
  NULL,
  NULL,
  'BIGINT exactness test'
);

DO $$
DECLARE
  _before_count BIGINT;
BEGIN
  IF (SELECT balance FROM public.gostudy_chalk_accounts
      WHERE userid = 918000000000003) <> 9223372036854775807 THEN
    RAISE EXCEPTION 'BIGINT exactness assertion failed';
  END IF;

  SELECT count(*) INTO _before_count
  FROM public.gostudy_chalk_transactions
  WHERE userid = 918000000000003;

  BEGIN
    PERFORM public.gostudy_apply_chalk_transaction(
      918000000000003, 1, 'system_adjustment',
      'chalk-sql:positive-overflow', NULL, NULL, NULL, NULL,
      'Positive overflow test'
    );
    RAISE EXCEPTION 'positive overflow unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22003' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_apply_chalk_transaction(
      918000000000003, '-9223372036854775808'::BIGINT,
      'system_adjustment', 'chalk-sql:bigint-min-debit',
      NULL, NULL, NULL, NULL, 'BIGINT minimum debit test'
    );
    RAISE EXCEPTION 'BIGINT minimum debit unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22003' THEN NULL;
  END;

  IF (SELECT balance FROM public.gostudy_chalk_accounts
      WHERE userid = 918000000000003) <> 9223372036854775807
     OR (SELECT count(*) FROM public.gostudy_chalk_transactions
         WHERE userid = 918000000000003) <> _before_count THEN
    RAISE EXCEPTION 'BIGINT failure was not atomic';
  END IF;
END;
$$;

-- Valid partial refund followed by cumulative over-refund rejection.
SELECT * FROM public.gostudy_apply_chalk_transaction(
  918000000000004, 20, 'admin_grant', 'chalk-sql:refund-funding',
  918000000009999, NULL, NULL, NULL, 'Refund funding test'
);

SELECT * FROM public.gostudy_apply_chalk_transaction(
  918000000000004, -10, 'shop_purchase', 'chalk-sql:purchase-10',
  NULL, 'board_purchase', 'purchase-10', NULL, NULL
);

DO $$
DECLARE
  _purchase_id BIGINT;
BEGIN
  SELECT transactionid INTO _purchase_id
  FROM public.gostudy_chalk_transactions
  WHERE idempotency_key = 'chalk-sql:purchase-10';

  PERFORM public.gostudy_apply_chalk_transaction(
    918000000000004, 4, 'refund', 'chalk-sql:refund-4',
    NULL, NULL, NULL, _purchase_id, NULL
  );

  IF (SELECT balance FROM public.gostudy_chalk_accounts
      WHERE userid = 918000000000004) <> 14 THEN
    RAISE EXCEPTION 'partial refund assertion failed';
  END IF;

  BEGIN
    PERFORM public.gostudy_apply_chalk_transaction(
      918000000000004, 7, 'refund', 'chalk-sql:refund-over',
      NULL, NULL, NULL, _purchase_id, NULL
    );
    RAISE EXCEPTION 'cumulative over-refund unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;
END;
$$;

-- Foreign-user and non-purchase refund targets fail.
SELECT * FROM public.gostudy_apply_chalk_transaction(
  918000000000005, 10, 'admin_grant', 'chalk-sql:foreign-funding',
  918000000009999, NULL, NULL, NULL, 'Foreign purchase funding'
);

SELECT * FROM public.gostudy_apply_chalk_transaction(
  918000000000005, -5, 'shop_purchase', 'chalk-sql:foreign-purchase',
  NULL, 'board_purchase', 'foreign-purchase', NULL, NULL
);

DO $$
DECLARE
  _foreign_purchase BIGINT;
  _non_purchase BIGINT;
BEGIN
  SELECT transactionid INTO _foreign_purchase
  FROM public.gostudy_chalk_transactions
  WHERE idempotency_key = 'chalk-sql:foreign-purchase';

  SELECT transactionid INTO _non_purchase
  FROM public.gostudy_chalk_transactions
  WHERE idempotency_key = 'chalk-sql:refund-funding';

  BEGIN
    PERFORM public.gostudy_apply_chalk_transaction(
      918000000000004, 1, 'refund', 'chalk-sql:foreign-refund',
      NULL, NULL, NULL, _foreign_purchase, NULL
    );
    RAISE EXCEPTION 'foreign-user refund unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_apply_chalk_transaction(
      918000000000004, 1, 'refund', 'chalk-sql:non-purchase-refund',
      NULL, NULL, NULL, _non_purchase, NULL
    );
    RAISE EXCEPTION 'non-purchase refund unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;
END;
$$;

ROLLBACK;
