BEGIN;

LOCK TABLE public.VersionHistory IN SHARE ROW EXCLUSIVE MODE;

DO $$
  DECLARE
    _current_version INTEGER;
  BEGIN
    _current_version := (
      SELECT version
      FROM public.VersionHistory
      ORDER BY time DESC
      LIMIT 1
    );

    IF _current_version IS DISTINCT FROM 19 THEN
      RAISE EXCEPTION
        'Migration v19-v20 requires schema version 19; found %.',
        COALESCE(_current_version::TEXT, 'NULL');
    END IF;
  END;
$$ LANGUAGE PLPGSQL;

-- Go Study Chalk board purchase API {{{
CREATE FUNCTION public.gostudy_purchase_board_item_chalk(
  _userid BIGINT,
  _amount BIGINT,
  _idempotency_key TEXT,
  _purchase_reference TEXT
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
    IF _userid IS NULL OR _userid <= 0 THEN
      RAISE EXCEPTION 'Board purchase Chalk userid must be positive.'
        USING ERRCODE = '22023';
    END IF;

    IF _amount IS NULL OR _amount < 1 OR _amount > 1000000 THEN
      RAISE EXCEPTION 'Board purchase Chalk amount must be between 1 and 1000000.'
        USING ERRCODE = '22023';
    END IF;

    IF _idempotency_key IS NULL OR _idempotency_key !~ (
      '^shop:' || _userid::TEXT
      || ':[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    ) THEN
      RAISE EXCEPTION
        'Board purchase Chalk idempotency key must be shop:<userid>:<UUIDv4>.'
        USING ERRCODE = '22023';
    END IF;

    IF _purchase_reference IS NULL OR _purchase_reference !~
      '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    THEN
      RAISE EXCEPTION
        'Board purchase Chalk reference must be a lowercase UUIDv4.'
        USING ERRCODE = '22023';
    END IF;

    _negative_amount := 0::NUMERIC - _amount::NUMERIC;

    RETURN QUERY
    SELECT result.*
    FROM public.gostudy_apply_chalk_transaction(
      _userid,
      _negative_amount::BIGINT,
      'shop_purchase'::TEXT,
      _idempotency_key,
      NULL::BIGINT,
      'board_purchase'::TEXT,
      _purchase_reference,
      NULL::BIGINT,
      NULL::TEXT
    ) AS result;
  END;
$$;

REVOKE ALL ON FUNCTION public.gostudy_purchase_board_item_chalk(
  BIGINT, BIGINT, TEXT, TEXT
) FROM PUBLIC;
-- }}}

INSERT INTO public.VersionHistory (version, author)
VALUES (20, 'v19-v20 migration');

COMMIT;
