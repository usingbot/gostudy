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

    IF _current_version IS DISTINCT FROM 16 THEN
      RAISE EXCEPTION
        'Migration v16-v17 requires schema version 16; found %.',
        COALESCE(_current_version::TEXT, 'NULL');
    END IF;
  END;
$$ LANGUAGE PLPGSQL;

-- Durable transactional outbox for Discord reward notifications.
-- This table intentionally starts empty: historical hour rewards are not
-- backfilled, and only rewards inserted after the trigger is installed are
-- queued for delivery.
CREATE TABLE public.gostudy_reward_notifications(
  notificationid BIGSERIAL PRIMARY KEY,
  hour_rewardid BIGINT NOT NULL UNIQUE
    REFERENCES public.gostudy_hour_rewards (rewardid) ON DELETE RESTRICT,
  userid BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'delivered', 'failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at TIMESTAMPTZ DEFAULT now(),
  last_attempt_at TIMESTAMPTZ,
  claim_token UUID,
  claimed_by VARCHAR(128),
  lease_expires_at TIMESTAMPTZ,
  delivered_at TIMESTAMPTZ,
  failed_at TIMESTAMPTZ,
  last_failure_code VARCHAR(64),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT gostudy_reward_notifications_consistent_state CHECK (
    (
      status = 'pending'
      AND next_attempt_at IS NOT NULL
      AND claim_token IS NULL
      AND claimed_by IS NULL
      AND lease_expires_at IS NULL
      AND delivered_at IS NULL
      AND failed_at IS NULL
    )
    OR (
      status = 'processing'
      AND next_attempt_at IS NULL
      AND last_attempt_at IS NOT NULL
      AND claim_token IS NOT NULL
      AND claimed_by IS NOT NULL
      AND lease_expires_at IS NOT NULL
      AND delivered_at IS NULL
      AND failed_at IS NULL
    )
    OR (
      status = 'delivered'
      AND next_attempt_at IS NULL
      AND claim_token IS NULL
      AND claimed_by IS NULL
      AND lease_expires_at IS NULL
      AND delivered_at IS NOT NULL
      AND failed_at IS NULL
    )
    OR (
      status = 'failed'
      AND next_attempt_at IS NULL
      AND claim_token IS NULL
      AND claimed_by IS NULL
      AND lease_expires_at IS NULL
      AND delivered_at IS NULL
      AND failed_at IS NOT NULL
    )
  )
);

CREATE INDEX gostudy_reward_notifications_pending
  ON public.gostudy_reward_notifications (
    next_attempt_at, userid, notificationid
  )
  WHERE status = 'pending';

CREATE INDEX gostudy_reward_notifications_expired_claims
  ON public.gostudy_reward_notifications (
    lease_expires_at, notificationid
  )
  WHERE status = 'processing';

CREATE INDEX gostudy_reward_notifications_user_status
  ON public.gostudy_reward_notifications (
    userid, status, notificationid
  );

CREATE FUNCTION public.gostudy_enqueue_hour_reward_notification()
  RETURNS TRIGGER
AS $$
  BEGIN
    INSERT INTO public.gostudy_reward_notifications (
      hour_rewardid, userid
    ) VALUES (
      NEW.rewardid, NEW.userid
    )
    ON CONFLICT (hour_rewardid) DO NOTHING;

    RETURN NEW;
  END;
$$ LANGUAGE PLPGSQL;

CREATE TRIGGER gostudy_enqueue_hour_reward_notification
AFTER INSERT ON public.gostudy_hour_rewards
FOR EACH ROW EXECUTE FUNCTION public.gostudy_enqueue_hour_reward_notification();

INSERT INTO VersionHistory (version, author)
VALUES (17, 'v16-v17 migration');

COMMIT;
