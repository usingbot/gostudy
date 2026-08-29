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

    IF _current_version IS DISTINCT FROM 17 THEN
      RAISE EXCEPTION
        'Migration v17-v18 requires schema version 17; found %.',
        COALESCE(_current_version::TEXT, 'NULL');
    END IF;
  END;
$$ LANGUAGE PLPGSQL;

-- Chalk accounts are created lazily by the mutation function. This migration
-- intentionally creates no historical balances.
CREATE TABLE public.gostudy_chalk_accounts(
  userid BIGINT PRIMARY KEY,
  balance BIGINT NOT NULL DEFAULT 0,
  lifetime_credited BIGINT NOT NULL DEFAULT 0,
  lifetime_debited BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT gostudy_chalk_accounts_userid_positive
    CHECK (userid > 0),
  CONSTRAINT gostudy_chalk_accounts_balance_nonnegative
    CHECK (balance >= 0),
  CONSTRAINT gostudy_chalk_accounts_credited_nonnegative
    CHECK (lifetime_credited >= 0),
  CONSTRAINT gostudy_chalk_accounts_debited_nonnegative
    CHECK (lifetime_debited >= 0),
  CONSTRAINT gostudy_chalk_accounts_totals_consistent
    CHECK (
      balance::NUMERIC =
        lifetime_credited::NUMERIC - lifetime_debited::NUMERIC
    )
);

CREATE TABLE public.gostudy_chalk_transactions(
  transactionid BIGSERIAL PRIMARY KEY,
  userid BIGINT NOT NULL
    REFERENCES public.gostudy_chalk_accounts (userid) ON DELETE RESTRICT,
  amount BIGINT NOT NULL,
  balance_after BIGINT NOT NULL,
  transaction_type TEXT NOT NULL,
  idempotency_key VARCHAR(128) NOT NULL,
  actor_userid BIGINT,
  reference_type VARCHAR(64),
  reference_id VARCHAR(128),
  reversal_of_transactionid BIGINT
    REFERENCES public.gostudy_chalk_transactions (transactionid)
    ON DELETE RESTRICT,
  reason VARCHAR(500),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT gostudy_chalk_transactions_idempotency_unique
    UNIQUE (idempotency_key),
  CONSTRAINT gostudy_chalk_transactions_amount_nonzero
    CHECK (amount <> 0),
  CONSTRAINT gostudy_chalk_transactions_balance_nonnegative
    CHECK (balance_after >= 0),
  CONSTRAINT gostudy_chalk_transactions_actor_positive
    CHECK (actor_userid IS NULL OR actor_userid > 0),
  CONSTRAINT gostudy_chalk_transactions_idempotency_nonblank
    CHECK (
      idempotency_key <> ''
      AND idempotency_key = btrim(idempotency_key)
    ),
  CONSTRAINT gostudy_chalk_transactions_reference_pair
    CHECK ((reference_type IS NULL) = (reference_id IS NULL)),
  CONSTRAINT gostudy_chalk_transactions_reference_type_format
    CHECK (
      reference_type IS NULL
      OR reference_type ~ '^[a-z][a-z0-9_]{0,63}$'
    ),
  CONSTRAINT gostudy_chalk_transactions_reason_nonblank
    CHECK (reason IS NULL OR btrim(reason) <> ''),
  CONSTRAINT gostudy_chalk_transactions_type_direction
    CHECK (
      (
        transaction_type IN ('admin_grant', 'study_earning', 'refund')
        AND amount > 0
      )
      OR (
        transaction_type IN ('admin_deduct', 'shop_purchase')
        AND amount < 0
      )
      OR transaction_type IN ('migration_adjustment', 'system_adjustment')
    ),
  CONSTRAINT gostudy_chalk_transactions_admin_actor
    CHECK (
      transaction_type NOT IN ('admin_grant', 'admin_deduct')
      OR actor_userid IS NOT NULL
    ),
  CONSTRAINT gostudy_chalk_transactions_adjustment_reason
    CHECK (
      transaction_type NOT IN (
        'admin_grant',
        'admin_deduct',
        'migration_adjustment',
        'system_adjustment'
      )
      OR reason IS NOT NULL
    ),
  CONSTRAINT gostudy_chalk_transactions_source_required
    CHECK (
      transaction_type NOT IN ('study_earning', 'shop_purchase')
      OR reference_type IS NOT NULL
    ),
  CONSTRAINT gostudy_chalk_transactions_refund_reversal
    CHECK (
      transaction_type <> 'refund'
      OR reversal_of_transactionid IS NOT NULL
    )
);

CREATE INDEX gostudy_chalk_transactions_user_history
  ON public.gostudy_chalk_transactions (userid, transactionid DESC);

CREATE INDEX gostudy_chalk_transactions_reference
  ON public.gostudy_chalk_transactions (reference_type, reference_id)
  WHERE reference_type IS NOT NULL;

CREATE INDEX gostudy_chalk_transactions_reversal
  ON public.gostudy_chalk_transactions (reversal_of_transactionid)
  WHERE reversal_of_transactionid IS NOT NULL;

CREATE FUNCTION public.gostudy_reject_chalk_ledger_mutation()
  RETURNS TRIGGER
  LANGUAGE PLPGSQL
  SET search_path = pg_catalog
AS $$
  BEGIN
    RAISE EXCEPTION
      'Go Study Chalk transactions are immutable; create a compensating/reversal transaction instead.'
      USING ERRCODE = '55000';
  END;
$$;

CREATE TRIGGER gostudy_reject_chalk_ledger_update_delete
BEFORE UPDATE OR DELETE ON public.gostudy_chalk_transactions
FOR EACH ROW EXECUTE FUNCTION public.gostudy_reject_chalk_ledger_mutation();

CREATE TRIGGER gostudy_reject_chalk_ledger_truncate
BEFORE TRUNCATE ON public.gostudy_chalk_transactions
FOR EACH STATEMENT EXECUTE FUNCTION public.gostudy_reject_chalk_ledger_mutation();

CREATE FUNCTION public.gostudy_apply_chalk_transaction(
  _userid BIGINT,
  _amount BIGINT,
  _transaction_type TEXT,
  _idempotency_key TEXT,
  _actor_userid BIGINT DEFAULT NULL,
  _reference_type TEXT DEFAULT NULL,
  _reference_id TEXT DEFAULT NULL,
  _reversal_of_transactionid BIGINT DEFAULT NULL,
  _reason TEXT DEFAULT NULL
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
    _bigint_min CONSTANT NUMERIC := '-9223372036854775808'::NUMERIC;
    _bigint_max CONSTANT NUMERIC := '9223372036854775807'::NUMERIC;
    _existing public.gostudy_chalk_transactions%ROWTYPE;
    _account public.gostudy_chalk_accounts%ROWTYPE;
    _reversed public.gostudy_chalk_transactions%ROWTYPE;
    _created public.gostudy_chalk_transactions%ROWTYPE;
    _amount_numeric NUMERIC;
    _new_balance NUMERIC;
    _new_credited NUMERIC;
    _new_debited NUMERIC;
    _purchase_debit NUMERIC;
    _existing_refunds NUMERIC;
  BEGIN
    IF _userid IS NULL OR _userid <= 0 THEN
      RAISE EXCEPTION 'Chalk userid must be positive.'
        USING ERRCODE = '22023';
    END IF;

    IF _amount IS NULL OR _amount = 0 THEN
      RAISE EXCEPTION 'Chalk amount must be nonzero.'
        USING ERRCODE = '22023';
    END IF;

    IF _transaction_type IS NULL OR _transaction_type NOT IN (
      'admin_grant',
      'admin_deduct',
      'study_earning',
      'shop_purchase',
      'refund',
      'migration_adjustment',
      'system_adjustment'
    ) THEN
      RAISE EXCEPTION 'Invalid Chalk transaction type.'
        USING ERRCODE = '22023';
    END IF;

    IF _idempotency_key IS NULL
       OR char_length(_idempotency_key) = 0
       OR char_length(_idempotency_key) > 128
       OR _idempotency_key IS DISTINCT FROM btrim(_idempotency_key) THEN
      RAISE EXCEPTION 'Chalk idempotency key must be canonical and 1-128 characters.'
        USING ERRCODE = '22023';
    END IF;

    IF _actor_userid IS NOT NULL AND _actor_userid <= 0 THEN
      RAISE EXCEPTION 'Chalk actor userid must be positive when provided.'
        USING ERRCODE = '22023';
    END IF;

    IF (_reference_type IS NULL) IS DISTINCT FROM (_reference_id IS NULL) THEN
      RAISE EXCEPTION 'Chalk reference type and id must be provided together.'
        USING ERRCODE = '22023';
    END IF;

    IF _reference_type IS NOT NULL AND (
      char_length(_reference_type) = 0
      OR char_length(_reference_type) > 64
      OR _reference_type !~ '^[a-z][a-z0-9_]{0,63}$'
    ) THEN
      RAISE EXCEPTION 'Invalid Chalk reference type.'
        USING ERRCODE = '22023';
    END IF;

    IF _reference_id IS NOT NULL AND (
      char_length(_reference_id) = 0
      OR char_length(_reference_id) > 128
      OR _reference_id IS DISTINCT FROM btrim(_reference_id)
    ) THEN
      RAISE EXCEPTION 'Chalk reference id must be canonical and 1-128 characters.'
        USING ERRCODE = '22023';
    END IF;

    IF _reason IS NOT NULL AND (
      char_length(_reason) > 500 OR btrim(_reason) = ''
    ) THEN
      RAISE EXCEPTION 'Chalk reason must be nonblank and at most 500 characters.'
        USING ERRCODE = '22023';
    END IF;

    IF _reversal_of_transactionid IS NOT NULL
       AND _reversal_of_transactionid <= 0 THEN
      RAISE EXCEPTION 'Chalk reversal transaction id must be positive.'
        USING ERRCODE = '22023';
    END IF;

    IF (
      _transaction_type IN ('admin_grant', 'study_earning', 'refund')
      AND _amount <= 0
    ) OR (
      _transaction_type IN ('admin_deduct', 'shop_purchase')
      AND _amount >= 0
    ) THEN
      RAISE EXCEPTION 'Chalk amount direction does not match transaction type.'
        USING ERRCODE = '22023';
    END IF;

    IF _transaction_type IN ('admin_grant', 'admin_deduct')
       AND _actor_userid IS NULL THEN
      RAISE EXCEPTION 'Chalk admin adjustments require an actor userid.'
        USING ERRCODE = '22023';
    END IF;

    IF _transaction_type IN (
      'admin_grant',
      'admin_deduct',
      'migration_adjustment',
      'system_adjustment'
    ) AND _reason IS NULL THEN
      RAISE EXCEPTION 'Chalk adjustments require a reason.'
        USING ERRCODE = '22023';
    END IF;

    IF _transaction_type IN ('study_earning', 'shop_purchase')
       AND _reference_type IS NULL THEN
      RAISE EXCEPTION 'Chalk source transaction requires a reference.'
        USING ERRCODE = '22023';
    END IF;

    IF _transaction_type = 'refund'
       AND _reversal_of_transactionid IS NULL THEN
      RAISE EXCEPTION 'Chalk refunds require a purchase transaction reference.'
        USING ERRCODE = '22023';
    END IF;

    -- The advisory lock serializes the exact replay key across users. Hash
    -- collisions only add serialization; the exact UNIQUE key is authoritative.
    PERFORM pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended(_idempotency_key, 0)
    );

    SELECT transactions.* INTO _existing
    FROM public.gostudy_chalk_transactions AS transactions
    WHERE transactions.idempotency_key = _idempotency_key;

    IF FOUND THEN
      IF _existing.userid IS DISTINCT FROM _userid
         OR _existing.amount IS DISTINCT FROM _amount
         OR _existing.transaction_type IS DISTINCT FROM _transaction_type
         OR _existing.actor_userid IS DISTINCT FROM _actor_userid
         OR _existing.reference_type IS DISTINCT FROM _reference_type
         OR _existing.reference_id IS DISTINCT FROM _reference_id
         OR _existing.reversal_of_transactionid IS DISTINCT FROM _reversal_of_transactionid
         OR _existing.reason IS DISTINCT FROM _reason THEN
        RAISE EXCEPTION 'Chalk idempotency conflict: key already has a different payload.'
          USING ERRCODE = '22000';
      END IF;

      RETURN QUERY
      SELECT
        transactions.transactionid,
        transactions.userid,
        transactions.amount,
        transactions.balance_after,
        transactions.transaction_type,
        transactions.idempotency_key,
        transactions.actor_userid,
        transactions.reference_type,
        transactions.reference_id,
        transactions.reversal_of_transactionid,
        transactions.reason,
        transactions.created_at,
        accounts.balance,
        accounts.lifetime_credited,
        accounts.lifetime_debited,
        accounts.created_at,
        accounts.updated_at,
        TRUE
      FROM public.gostudy_chalk_transactions AS transactions
      JOIN public.gostudy_chalk_accounts AS accounts
        ON accounts.userid = transactions.userid
      WHERE transactions.transactionid = _existing.transactionid;
      RETURN;
    END IF;

    INSERT INTO public.gostudy_chalk_accounts (userid)
    VALUES (_userid)
    ON CONFLICT ON CONSTRAINT gostudy_chalk_accounts_pkey DO NOTHING;

    SELECT accounts.* INTO STRICT _account
    FROM public.gostudy_chalk_accounts AS accounts
    WHERE accounts.userid = _userid
    FOR UPDATE;

    IF _reversal_of_transactionid IS NOT NULL THEN
      SELECT transactions.* INTO _reversed
      FROM public.gostudy_chalk_transactions AS transactions
      WHERE transactions.transactionid = _reversal_of_transactionid
      FOR UPDATE;

      IF NOT FOUND THEN
        RAISE EXCEPTION 'Referenced Chalk reversal transaction does not exist.'
          USING ERRCODE = '22023';
      END IF;

      IF _reversed.userid IS DISTINCT FROM _userid THEN
        RAISE EXCEPTION 'Referenced Chalk reversal transaction belongs to another user.'
          USING ERRCODE = '22023';
      END IF;
    END IF;

    IF _transaction_type = 'refund' THEN
      IF _reversed.transaction_type IS DISTINCT FROM 'shop_purchase'
         OR _reversed.amount >= 0 THEN
        RAISE EXCEPTION 'Chalk refunds must reference a negative shop purchase.'
          USING ERRCODE = '22023';
      END IF;

      SELECT COALESCE(sum(refunds.amount::NUMERIC), 0::NUMERIC)
      INTO _existing_refunds
      FROM public.gostudy_chalk_transactions AS refunds
      WHERE refunds.transaction_type = 'refund'
        AND refunds.reversal_of_transactionid = _reversal_of_transactionid;

      _purchase_debit := 0::NUMERIC - _reversed.amount::NUMERIC;
      IF _existing_refunds + _amount::NUMERIC > _purchase_debit THEN
        RAISE EXCEPTION 'Chalk refund exceeds the remaining purchase debit.'
          USING ERRCODE = '22023';
      END IF;
    END IF;

    _amount_numeric := _amount::NUMERIC;
    _new_balance := _account.balance::NUMERIC + _amount_numeric;
    _new_credited := _account.lifetime_credited::NUMERIC
      + CASE WHEN _amount > 0 THEN _amount_numeric ELSE 0::NUMERIC END;
    _new_debited := _account.lifetime_debited::NUMERIC
      + CASE WHEN _amount < 0 THEN 0::NUMERIC - _amount_numeric ELSE 0::NUMERIC END;

    IF _new_balance < _bigint_min OR _new_balance > _bigint_max
       OR _new_credited < _bigint_min OR _new_credited > _bigint_max
       OR _new_debited < _bigint_min OR _new_debited > _bigint_max THEN
      RAISE EXCEPTION 'Chalk transaction exceeds BIGINT range.'
        USING ERRCODE = '22003';
    END IF;

    IF _new_balance < 0
       OR _new_credited < 0
       OR _new_debited < 0 THEN
      RAISE EXCEPTION 'Chalk balance cannot become negative.'
        USING ERRCODE = '23514';
    END IF;

    UPDATE public.gostudy_chalk_accounts AS accounts
    SET
      balance = _new_balance::BIGINT,
      lifetime_credited = _new_credited::BIGINT,
      lifetime_debited = _new_debited::BIGINT,
      updated_at = now()
    WHERE accounts.userid = _userid
    RETURNING accounts.* INTO STRICT _account;

    INSERT INTO public.gostudy_chalk_transactions (
      userid,
      amount,
      balance_after,
      transaction_type,
      idempotency_key,
      actor_userid,
      reference_type,
      reference_id,
      reversal_of_transactionid,
      reason
    ) VALUES (
      _userid,
      _amount,
      _new_balance::BIGINT,
      _transaction_type,
      _idempotency_key,
      _actor_userid,
      _reference_type,
      _reference_id,
      _reversal_of_transactionid,
      _reason
    )
    RETURNING * INTO STRICT _created;

    RETURN QUERY
    SELECT
      transactions.transactionid,
      transactions.userid,
      transactions.amount,
      transactions.balance_after,
      transactions.transaction_type,
      transactions.idempotency_key,
      transactions.actor_userid,
      transactions.reference_type,
      transactions.reference_id,
      transactions.reversal_of_transactionid,
      transactions.reason,
      transactions.created_at,
      accounts.balance,
      accounts.lifetime_credited,
      accounts.lifetime_debited,
      accounts.created_at,
      accounts.updated_at,
      FALSE
    FROM public.gostudy_chalk_transactions AS transactions
    JOIN public.gostudy_chalk_accounts AS accounts
      ON accounts.userid = transactions.userid
    WHERE transactions.transactionid = _created.transactionid;
  END;
$$;

REVOKE ALL ON FUNCTION public.gostudy_apply_chalk_transaction(
  BIGINT, BIGINT, TEXT, TEXT, BIGINT, TEXT, TEXT, BIGINT, TEXT
) FROM PUBLIC;

REVOKE ALL ON FUNCTION public.gostudy_reject_chalk_ledger_mutation()
  FROM PUBLIC;

REVOKE ALL ON TABLE
  public.gostudy_chalk_accounts,
  public.gostudy_chalk_transactions
FROM PUBLIC;

REVOKE ALL ON SEQUENCE
  public.gostudy_chalk_transactions_transactionid_seq
FROM PUBLIC;

INSERT INTO VersionHistory (version, author)
VALUES (18, 'v17-v18 migration');

COMMIT;
