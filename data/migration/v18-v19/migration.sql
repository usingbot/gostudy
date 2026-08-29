BEGIN;

LOCK TABLE VersionHistory IN SHARE ROW EXCLUSIVE MODE;

DO $$
  DECLARE
    _current_version INTEGER;
  BEGIN
    _current_version := (
      SELECT version
      FROM VersionHistory
      ORDER BY time DESC
      LIMIT 1
    );

    IF _current_version IS DISTINCT FROM 18 THEN
      RAISE EXCEPTION
        'Migration v18-v19 requires schema version 18; found %.',
        COALESCE(_current_version::TEXT, 'NULL');
    END IF;
  END;
$$ LANGUAGE PLPGSQL;

-- Go Study Chalk admin API {{{
-- These functions deliberately constrain the future web API without granting
-- any application role access in the StudyLion schema migration.
CREATE FUNCTION public.gostudy_admin_grant_chalk(
  _target_userid BIGINT,
  _actor_userid BIGINT,
  _amount BIGINT,
  _idempotency_key TEXT,
  _reason TEXT
)
RETURNS TABLE(
  transactionid BIGINT,
  userid BIGINT,
  amount BIGINT,
  balance_after BIGINT,
  transaction_type TEXT,
  idempotency_key VARCHAR(128),
  actor_userid BIGINT,
  reference_type VARCHAR(64),
  reference_id VARCHAR(128),
  reversal_of_transactionid BIGINT,
  reason VARCHAR(500),
  created_at TIMESTAMPTZ,
  account_balance BIGINT,
  account_lifetime_credited BIGINT,
  account_lifetime_debited BIGINT,
  account_created_at TIMESTAMPTZ,
  account_updated_at TIMESTAMPTZ,
  replayed BOOLEAN
)
LANGUAGE PLPGSQL
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  BEGIN
    IF _target_userid IS NULL OR _target_userid <= 0 THEN
      RAISE EXCEPTION 'Admin Chalk target userid must be positive.'
        USING ERRCODE = '22023';
    END IF;

    IF _actor_userid IS NULL OR _actor_userid <= 0 THEN
      RAISE EXCEPTION 'Admin Chalk actor userid must be positive.'
        USING ERRCODE = '22023';
    END IF;

    IF _amount IS NULL OR _amount < 1 OR _amount > 1000000 THEN
      RAISE EXCEPTION 'Admin Chalk amount must be between 1 and 1000000.'
        USING ERRCODE = '22023';
    END IF;

    IF _reason IS NULL
       OR char_length(_reason) < 1
       OR char_length(_reason) > 500
       OR _reason IS DISTINCT FROM btrim(_reason) THEN
      RAISE EXCEPTION 'Admin Chalk reason must be canonical and 1-500 characters.'
        USING ERRCODE = '22023';
    END IF;

    IF _idempotency_key IS NULL OR _idempotency_key !~ (
      '^admin:' || _actor_userid::TEXT
      || ':[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    ) THEN
      RAISE EXCEPTION
        'Admin Chalk idempotency key must be admin:<actor_userid>:<UUIDv4>.'
        USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT result.*
    FROM public.gostudy_apply_chalk_transaction(
      _target_userid,
      _amount,
      'admin_grant'::TEXT,
      _idempotency_key,
      _actor_userid,
      NULL::TEXT,
      NULL::TEXT,
      NULL::BIGINT,
      _reason
    ) AS result;
  END;
$$;

CREATE FUNCTION public.gostudy_admin_deduct_chalk(
  _target_userid BIGINT,
  _actor_userid BIGINT,
  _amount BIGINT,
  _idempotency_key TEXT,
  _reason TEXT
)
RETURNS TABLE(
  transactionid BIGINT,
  userid BIGINT,
  amount BIGINT,
  balance_after BIGINT,
  transaction_type TEXT,
  idempotency_key VARCHAR(128),
  actor_userid BIGINT,
  reference_type VARCHAR(64),
  reference_id VARCHAR(128),
  reversal_of_transactionid BIGINT,
  reason VARCHAR(500),
  created_at TIMESTAMPTZ,
  account_balance BIGINT,
  account_lifetime_credited BIGINT,
  account_lifetime_debited BIGINT,
  account_created_at TIMESTAMPTZ,
  account_updated_at TIMESTAMPTZ,
  replayed BOOLEAN
)
LANGUAGE PLPGSQL
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  DECLARE
    _negative_amount NUMERIC;
  BEGIN
    IF _target_userid IS NULL OR _target_userid <= 0 THEN
      RAISE EXCEPTION 'Admin Chalk target userid must be positive.'
        USING ERRCODE = '22023';
    END IF;

    IF _actor_userid IS NULL OR _actor_userid <= 0 THEN
      RAISE EXCEPTION 'Admin Chalk actor userid must be positive.'
        USING ERRCODE = '22023';
    END IF;

    IF _amount IS NULL OR _amount < 1 OR _amount > 1000000 THEN
      RAISE EXCEPTION 'Admin Chalk amount must be between 1 and 1000000.'
        USING ERRCODE = '22023';
    END IF;

    IF _reason IS NULL
       OR char_length(_reason) < 1
       OR char_length(_reason) > 500
       OR _reason IS DISTINCT FROM btrim(_reason) THEN
      RAISE EXCEPTION 'Admin Chalk reason must be canonical and 1-500 characters.'
        USING ERRCODE = '22023';
    END IF;

    IF _idempotency_key IS NULL OR _idempotency_key !~ (
      '^admin:' || _actor_userid::TEXT
      || ':[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    ) THEN
      RAISE EXCEPTION
        'Admin Chalk idempotency key must be admin:<actor_userid>:<UUIDv4>.'
        USING ERRCODE = '22023';
    END IF;

    _negative_amount := 0::NUMERIC - _amount::NUMERIC;

    RETURN QUERY
    SELECT result.*
    FROM public.gostudy_apply_chalk_transaction(
      _target_userid,
      _negative_amount::BIGINT,
      'admin_deduct'::TEXT,
      _idempotency_key,
      _actor_userid,
      NULL::TEXT,
      NULL::TEXT,
      NULL::BIGINT,
      _reason
    ) AS result;
  END;
$$;

CREATE FUNCTION public.gostudy_admin_get_chalk_account(
  _target_userid BIGINT
)
RETURNS TABLE(
  userid BIGINT,
  balance BIGINT,
  lifetime_credited BIGINT,
  lifetime_debited BIGINT,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
)
LANGUAGE PLPGSQL
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  BEGIN
    IF _target_userid IS NULL OR _target_userid <= 0 THEN
      RAISE EXCEPTION 'Admin Chalk target userid must be positive.'
        USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT
      _target_userid,
      COALESCE(accounts.balance, 0::BIGINT),
      COALESCE(accounts.lifetime_credited, 0::BIGINT),
      COALESCE(accounts.lifetime_debited, 0::BIGINT),
      accounts.created_at,
      accounts.updated_at
    FROM (SELECT 1) AS one
    LEFT JOIN public.gostudy_chalk_accounts AS accounts
      ON accounts.userid = _target_userid;
  END;
$$;

CREATE FUNCTION public.gostudy_admin_list_chalk_transactions(
  _target_userid BIGINT,
  _before_transactionid BIGINT DEFAULT NULL,
  _limit INTEGER DEFAULT 20
)
RETURNS TABLE(
  transactionid BIGINT,
  userid BIGINT,
  amount BIGINT,
  balance_after BIGINT,
  transaction_type TEXT,
  actor_userid BIGINT,
  reason VARCHAR(500),
  created_at TIMESTAMPTZ
)
LANGUAGE PLPGSQL
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  BEGIN
    IF _target_userid IS NULL OR _target_userid <= 0 THEN
      RAISE EXCEPTION 'Admin Chalk target userid must be positive.'
        USING ERRCODE = '22023';
    END IF;

    IF _before_transactionid IS NOT NULL
       AND _before_transactionid <= 0 THEN
      RAISE EXCEPTION 'Admin Chalk history cursor must be positive when provided.'
        USING ERRCODE = '22023';
    END IF;

    IF _limit IS NULL OR _limit < 1 OR _limit > 50 THEN
      RAISE EXCEPTION 'Admin Chalk history limit must be between 1 and 50.'
        USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT
      transactions.transactionid,
      transactions.userid,
      transactions.amount,
      transactions.balance_after,
      transactions.transaction_type,
      transactions.actor_userid,
      transactions.reason,
      transactions.created_at
    FROM public.gostudy_chalk_transactions AS transactions
    WHERE transactions.userid = _target_userid
      AND (
        _before_transactionid IS NULL
        OR transactions.transactionid < _before_transactionid
      )
    ORDER BY transactions.transactionid DESC
    LIMIT _limit;
  END;
$$;

REVOKE ALL ON FUNCTION public.gostudy_admin_grant_chalk(
  BIGINT, BIGINT, BIGINT, TEXT, TEXT
) FROM PUBLIC;

REVOKE ALL ON FUNCTION public.gostudy_admin_deduct_chalk(
  BIGINT, BIGINT, BIGINT, TEXT, TEXT
) FROM PUBLIC;

REVOKE ALL ON FUNCTION public.gostudy_admin_get_chalk_account(BIGINT)
  FROM PUBLIC;

REVOKE ALL ON FUNCTION public.gostudy_admin_list_chalk_transactions(
  BIGINT, BIGINT, INTEGER
) FROM PUBLIC;
-- }}}

INSERT INTO VersionHistory (version, author)
VALUES (19, 'v18-v19 migration');

COMMIT;
