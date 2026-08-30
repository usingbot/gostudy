-- Run only with psql against an explicitly authorized disposable schema v21
-- database. Every write in this sequential suite is rolled back.
BEGIN;

-- A missing account is returned as zero without creating persistent state.
DO $$
DECLARE
  _account RECORD;
BEGIN
  SELECT * INTO STRICT _account
  FROM public.gostudy_admin_get_chalk_account(918000000000102);

  IF _account.userid <> 918000000000102
     OR _account.balance <> 0
     OR _account.lifetime_credited <> 0
     OR _account.lifetime_debited <> 0
     OR _account.created_at IS NOT NULL
     OR _account.updated_at IS NOT NULL
     OR EXISTS (
       SELECT 1 FROM public.gostudy_chalk_accounts
       WHERE userid = 918000000000102
     ) THEN
    RAISE EXCEPTION 'missing admin account read assertion failed';
  END IF;
END;
$$;

-- Wrappers hardcode their transaction types and preserve the v18 result shape.
SELECT * FROM public.gostudy_admin_grant_chalk(
  918000000000101,
  918000000009999,
  10,
  'admin:918000000009999:11111111-1111-4111-8111-111111111111',
  'Admin wrapper grant test'
);

SELECT * FROM public.gostudy_admin_deduct_chalk(
  918000000000101,
  918000000009999,
  3,
  'admin:918000000009999:22222222-2222-4222-8222-222222222222',
  'Admin wrapper deduct test'
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM public.gostudy_chalk_transactions
    WHERE idempotency_key =
      'admin:918000000009999:11111111-1111-4111-8111-111111111111'
      AND userid = 918000000000101
      AND amount = 10
      AND transaction_type = 'admin_grant'
      AND actor_userid = 918000000009999
      AND reference_type IS NULL
      AND reference_id IS NULL
      AND reversal_of_transactionid IS NULL
      AND reason = 'Admin wrapper grant test'
  ) OR NOT EXISTS (
    SELECT 1
    FROM public.gostudy_chalk_transactions
    WHERE idempotency_key =
      'admin:918000000009999:22222222-2222-4222-8222-222222222222'
      AND userid = 918000000000101
      AND amount = -3
      AND transaction_type = 'admin_deduct'
      AND actor_userid = 918000000009999
      AND reference_type IS NULL
      AND reference_id IS NULL
      AND reversal_of_transactionid IS NULL
      AND reason = 'Admin wrapper deduct test'
  ) OR NOT EXISTS (
    SELECT 1
    FROM public.gostudy_chalk_accounts
    WHERE userid = 918000000000101
      AND balance = 7
      AND lifetime_credited = 10
      AND lifetime_debited = 3
  ) THEN
    RAISE EXCEPTION 'admin wrapper transaction assertion failed';
  END IF;
END;
$$;

-- Exact replay remains idempotent; changed payload still conflicts in v18.
DO $$
DECLARE
  _replayed BOOLEAN;
BEGIN
  SELECT result.replayed INTO STRICT _replayed
  FROM public.gostudy_admin_grant_chalk(
    918000000000101,
    918000000009999,
    10,
    'admin:918000000009999:11111111-1111-4111-8111-111111111111',
    'Admin wrapper grant test'
  ) AS result;

  IF _replayed IS NOT TRUE
     OR (SELECT count(*) FROM public.gostudy_chalk_transactions
         WHERE idempotency_key =
           'admin:918000000009999:11111111-1111-4111-8111-111111111111') <> 1
     OR (SELECT balance FROM public.gostudy_chalk_accounts
         WHERE userid = 918000000000101) <> 7 THEN
    RAISE EXCEPTION 'admin wrapper replay assertion failed';
  END IF;

  BEGIN
    PERFORM public.gostudy_admin_grant_chalk(
      918000000000101,
      918000000009999,
      11,
      'admin:918000000009999:11111111-1111-4111-8111-111111111111',
      'Admin wrapper grant test'
    );
    RAISE EXCEPTION 'changed wrapper payload unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22000' THEN
    NULL;
  END;
END;
$$;

-- Insufficient balance rolls back both account and ledger changes.
DO $$
DECLARE
  _before_count BIGINT;
BEGIN
  SELECT count(*) INTO _before_count
  FROM public.gostudy_chalk_transactions
  WHERE userid = 918000000000101;

  BEGIN
    PERFORM public.gostudy_admin_deduct_chalk(
      918000000000101,
      918000000009999,
      8,
      'admin:918000000009999:33333333-3333-4333-8333-333333333333',
      'Admin wrapper overspend test'
    );
    RAISE EXCEPTION 'admin wrapper overspend unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '23514' THEN
    NULL;
  END;

  IF (SELECT balance FROM public.gostudy_chalk_accounts
      WHERE userid = 918000000000101) <> 7
     OR (SELECT count(*) FROM public.gostudy_chalk_transactions
         WHERE userid = 918000000000101) <> _before_count THEN
    RAISE EXCEPTION 'admin wrapper overspend was not atomic';
  END IF;
END;
$$;

-- Wrapper input validation rejects invalid IDs, amounts, reasons, and keys.
DO $$
BEGIN
  BEGIN
    PERFORM public.gostudy_admin_grant_chalk(
      0, 918000000009999, 1,
      'admin:918000000009999:44444444-4444-4444-8444-444444444444',
      'Invalid target test'
    );
    RAISE EXCEPTION 'zero target unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_admin_grant_chalk(
      918000000000101, 0, 1,
      'admin:0:44444444-4444-4444-8444-444444444444',
      'Invalid actor test'
    );
    RAISE EXCEPTION 'zero actor unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_admin_grant_chalk(
      918000000000101, 918000000009999, 0,
      'admin:918000000009999:44444444-4444-4444-8444-444444444444',
      'Zero amount test'
    );
    RAISE EXCEPTION 'zero amount unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_admin_deduct_chalk(
      918000000000101, 918000000009999, 1000001,
      'admin:918000000009999:44444444-4444-4444-8444-444444444444',
      'Oversized amount test'
    );
    RAISE EXCEPTION 'oversized amount unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_admin_grant_chalk(
      918000000000101, 918000000009999, 1,
      'admin:918000000009999:44444444-4444-4444-8444-444444444444',
      NULL
    );
    RAISE EXCEPTION 'NULL reason unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_admin_grant_chalk(
      918000000000101, 918000000009999, 1,
      'admin:918000000009999:44444444-4444-4444-8444-444444444444',
      ' leading whitespace'
    );
    RAISE EXCEPTION 'noncanonical reason unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_admin_grant_chalk(
      918000000000101, 918000000009999, 1,
      'admin:918000000009999:44444444-4444-4444-8444-444444444444',
      repeat('x', 501)
    );
    RAISE EXCEPTION 'oversized reason unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_admin_grant_chalk(
      918000000000101, 918000000009999, 1,
      'not-an-admin-key',
      'Malformed key test'
    );
    RAISE EXCEPTION 'malformed idempotency key unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_admin_grant_chalk(
      918000000000101, 918000000009999, 1,
      'admin:918000000009998:44444444-4444-4444-8444-444444444444',
      'Actor mismatch key test'
    );
    RAISE EXCEPTION 'mismatched key actor unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;
END;
$$;

-- History is bounded, descending, and uses transaction-id keyset pagination.
DO $$
DECLARE
  _latest BIGINT;
  _older BIGINT;
BEGIN
  SELECT transactionid INTO STRICT _latest
  FROM public.gostudy_admin_list_chalk_transactions(
    918000000000101, NULL, 1
  );

  SELECT transactionid INTO STRICT _older
  FROM public.gostudy_admin_list_chalk_transactions(
    918000000000101, _latest, 1
  );

  IF _older >= _latest
     OR (SELECT transaction_type FROM public.gostudy_chalk_transactions
         WHERE transactionid = _latest) <> 'admin_deduct'
     OR (SELECT transaction_type FROM public.gostudy_chalk_transactions
         WHERE transactionid = _older) <> 'admin_grant' THEN
    RAISE EXCEPTION 'admin history keyset assertion failed';
  END IF;

  BEGIN
    PERFORM public.gostudy_admin_list_chalk_transactions(
      918000000000101, 0, 20
    );
    RAISE EXCEPTION 'zero history cursor unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_admin_list_chalk_transactions(
      918000000000101, NULL, 51
    );
    RAISE EXCEPTION 'oversized history limit unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;
END;
$$;

ROLLBACK;
