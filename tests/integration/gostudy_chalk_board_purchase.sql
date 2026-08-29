-- Run only with psql against an explicitly authorized disposable schema v20
-- database. Every write in this sequential suite is rolled back.
BEGIN;

-- Seed a balance through the existing private primitive.
SELECT * FROM public.gostudy_apply_chalk_transaction(
  920000000000101,
  30,
  'admin_grant',
  'chalk-board-sql:funding',
  920000000009999,
  NULL,
  NULL,
  NULL,
  'Board purchase wrapper funding'
);

-- The wrapper hardcodes the complete board-purchase payload and returns the
-- canonical transaction/account result.
DO $$
DECLARE
  _result RECORD;
BEGIN
  SELECT * INTO STRICT _result
  FROM public.gostudy_purchase_board_item_chalk(
    920000000000101,
    7,
    'shop:920000000000101:11111111-1111-4111-8111-111111111111',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
  );

  IF _result.userid <> 920000000000101
     OR _result.amount <> -7
     OR _result.balance_after <> 23
     OR _result.transaction_type <> 'shop_purchase'
     OR _result.actor_userid IS NOT NULL
     OR _result.reference_type <> 'board_purchase'
     OR _result.reference_id <> 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
     OR _result.reversal_of_transactionid IS NOT NULL
     OR _result.reason IS NOT NULL
     OR _result.account_balance <> 23
     OR _result.account_lifetime_credited <> 30
     OR _result.account_lifetime_debited <> 7
     OR _result.replayed IS NOT FALSE THEN
    RAISE EXCEPTION 'canonical board purchase result assertion failed';
  END IF;
END;
$$;

-- Exact replay applies once; a changed payload uses the primitive conflict.
DO $$
DECLARE
  _result RECORD;
BEGIN
  SELECT * INTO STRICT _result
  FROM public.gostudy_purchase_board_item_chalk(
    920000000000101,
    7,
    'shop:920000000000101:11111111-1111-4111-8111-111111111111',
    'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
  );

  IF _result.replayed IS NOT TRUE
     OR (SELECT count(*) FROM public.gostudy_chalk_transactions
         WHERE idempotency_key =
           'shop:920000000000101:11111111-1111-4111-8111-111111111111') <> 1
     OR (SELECT balance FROM public.gostudy_chalk_accounts
         WHERE userid = 920000000000101) <> 23 THEN
    RAISE EXCEPTION 'board purchase replay assertion failed';
  END IF;

  BEGIN
    PERFORM public.gostudy_purchase_board_item_chalk(
      920000000000101,
      8,
      'shop:920000000000101:11111111-1111-4111-8111-111111111111',
      'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    );
    RAISE EXCEPTION 'changed board purchase payload unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22000' THEN
    NULL;
  END;
END;
$$;

-- Wrapper bounds and both canonical UUIDv4 formats are enforced.
DO $$
BEGIN
  BEGIN
    PERFORM public.gostudy_purchase_board_item_chalk(
      0, 1,
      'shop:0:22222222-2222-4222-8222-222222222222',
      'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    );
    RAISE EXCEPTION 'invalid board purchase userid unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_purchase_board_item_chalk(
      920000000000101, 0,
      'shop:920000000000101:22222222-2222-4222-8222-222222222222',
      'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    );
    RAISE EXCEPTION 'zero board purchase amount unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_purchase_board_item_chalk(
      920000000000101, 1000001,
      'shop:920000000000101:22222222-2222-4222-8222-222222222222',
      'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    );
    RAISE EXCEPTION 'oversized board purchase amount unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_purchase_board_item_chalk(
      920000000000101, 1,
      'shop:920000000000101:22222222-2222-1222-8222-222222222222',
      'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    );
    RAISE EXCEPTION 'malformed board request UUID unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_purchase_board_item_chalk(
      920000000000101, 1,
      'shop:920000000000102:22222222-2222-4222-8222-222222222222',
      'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    );
    RAISE EXCEPTION 'mismatched board request userid unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;

  BEGIN
    PERFORM public.gostudy_purchase_board_item_chalk(
      920000000000101, 1,
      'shop:920000000000101:22222222-2222-4222-8222-222222222222',
      'BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB'
    );
    RAISE EXCEPTION 'noncanonical purchase reference unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '22023' THEN NULL;
  END;
END;
$$;

-- Insufficient balance preserves v18 SQLSTATE 23514 and leaves no account or
-- ledger row behind for a previously unseen user.
DO $$
BEGIN
  BEGIN
    PERFORM public.gostudy_purchase_board_item_chalk(
      920000000000102,
      1,
      'shop:920000000000102:33333333-3333-4333-8333-333333333333',
      'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
    );
    RAISE EXCEPTION 'unfunded board purchase unexpectedly succeeded';
  EXCEPTION WHEN SQLSTATE '23514' THEN
    NULL;
  END;

  IF EXISTS (
    SELECT 1 FROM public.gostudy_chalk_accounts
    WHERE userid = 920000000000102
  ) OR EXISTS (
    SELECT 1 FROM public.gostudy_chalk_transactions
    WHERE userid = 920000000000102
  ) THEN
    RAISE EXCEPTION 'unfunded board purchase left a partial write';
  END IF;
END;
$$;

-- PUBLIC retains no EXECUTE privilege on the wrapper.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_proc AS functions
    CROSS JOIN LATERAL pg_catalog.aclexplode(
      COALESCE(
        functions.proacl,
        pg_catalog.acldefault('f', functions.proowner)
      )
    ) AS privileges
    WHERE functions.oid = (
      'public.gostudy_purchase_board_item_chalk(bigint,bigint,text,text)'
    )::REGPROCEDURE
      AND privileges.grantee = 0
      AND privileges.privilege_type = 'EXECUTE'
  ) THEN
    RAISE EXCEPTION 'PUBLIC can execute the board purchase wrapper';
  END IF;
END;
$$;

ROLLBACK;
