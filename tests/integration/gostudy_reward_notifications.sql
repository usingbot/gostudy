-- Run with psql against a disposable database after applying schema v20.
-- Every write is rolled back.
BEGIN;

INSERT INTO gostudy_reward_accounts (userid, verified_seconds)
VALUES (-17000, 10800);

INSERT INTO gostudy_verified_session_credits (
  source_sessionid, userid, source_guildid, verified_seconds
) VALUES
  (-17001, -17000, -17000, 3600),
  (-17002, -17000, -17000, 3600),
  (-17003, -17000, -17000, 3600);

-- Simulate a reward that predates trigger installation. Enabling the trigger
-- does not scan or backfill the existing entitlement.
ALTER TABLE gostudy_hour_rewards
  DISABLE TRIGGER gostudy_enqueue_hour_reward_notification;

INSERT INTO gostudy_hour_rewards (
  userid, milestone_hour, source_sessionid, verified_seconds_at_award
) VALUES (-17000, 1, -17001, 3600);

ALTER TABLE gostudy_hour_rewards
  ENABLE TRIGGER gostudy_enqueue_hour_reward_notification;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM gostudy_reward_notifications
    WHERE userid = -17000
  ) THEN
    RAISE EXCEPTION 'historical reward was unexpectedly backfilled';
  END IF;
END;
$$;

-- A post-v17 reward produces exactly one outbox row in the reward transaction.
INSERT INTO gostudy_hour_rewards (
  userid, milestone_hour, source_sessionid, verified_seconds_at_award
) VALUES (-17000, 2, -17002, 7200);

DO $$
BEGIN
  IF (
    SELECT COUNT(*)
    FROM gostudy_reward_notifications AS notifications
    JOIN gostudy_hour_rewards AS rewards
      ON rewards.rewardid = notifications.hour_rewardid
    WHERE rewards.userid = -17000 AND rewards.milestone_hour = 2
  ) <> 1 THEN
    RAISE EXCEPTION 'new reward did not create exactly one outbox row';
  END IF;
END;
$$;

-- Replaying both entitlement creation and the enqueue operation remains
-- idempotent for the durable hour_rewardid key.
INSERT INTO gostudy_hour_rewards (
  userid, milestone_hour, source_sessionid, verified_seconds_at_award
) VALUES (-17000, 2, -17002, 7200)
ON CONFLICT (userid, milestone_hour) DO NOTHING;

INSERT INTO gostudy_reward_notifications (hour_rewardid, userid)
SELECT rewardid, userid
FROM gostudy_hour_rewards
WHERE userid = -17000 AND milestone_hour = 2
ON CONFLICT (hour_rewardid) DO NOTHING;

DO $$
BEGIN
  IF (
    SELECT COUNT(*)
    FROM gostudy_reward_notifications
    WHERE userid = -17000
  ) <> 1 THEN
    RAISE EXCEPTION 'outbox idempotency assertion failed';
  END IF;
END;
$$;

-- Reserve an entitlement without an automatic outbox row for direct
-- constraint assertions.
ALTER TABLE gostudy_hour_rewards
  DISABLE TRIGGER gostudy_enqueue_hour_reward_notification;

INSERT INTO gostudy_hour_rewards (
  userid, milestone_hour, source_sessionid, verified_seconds_at_award
) VALUES (-17000, 3, -17003, 10800);

ALTER TABLE gostudy_hour_rewards
  ENABLE TRIGGER gostudy_enqueue_hour_reward_notification;

DO $$
DECLARE
  _rewardid BIGINT;
BEGIN
  SELECT rewardid INTO _rewardid
  FROM gostudy_hour_rewards
  WHERE userid = -17000 AND milestone_hour = 3;

  BEGIN
    INSERT INTO gostudy_reward_notifications (
      hour_rewardid,
      userid,
      status,
      next_attempt_at
    ) VALUES (
      _rewardid,
      -17000,
      'processing',
      NULL
    );
    RAISE EXCEPTION 'inconsistent processing state was accepted';
  EXCEPTION
    WHEN check_violation THEN NULL;
  END;

  BEGIN
    INSERT INTO gostudy_reward_notifications (
      hour_rewardid,
      userid,
      status,
      next_attempt_at
    ) VALUES (
      _rewardid,
      -17000,
      'pending',
      NULL
    );
    RAISE EXCEPTION 'inconsistent pending state was accepted';
  EXCEPTION
    WHEN check_violation THEN NULL;
  END;

  BEGIN
    INSERT INTO gostudy_reward_notifications (
      hour_rewardid,
      userid,
      status,
      next_attempt_at
    ) VALUES (
      _rewardid,
      -17000,
      'delivered',
      NULL
    );
    RAISE EXCEPTION 'inconsistent delivered state was accepted';
  EXCEPTION
    WHEN check_violation THEN NULL;
  END;

  BEGIN
    INSERT INTO gostudy_reward_notifications (
      hour_rewardid,
      userid,
      status,
      next_attempt_at
    ) VALUES (
      _rewardid,
      -17000,
      'failed',
      NULL
    );
    RAISE EXCEPTION 'inconsistent failed state was accepted';
  EXCEPTION
    WHEN check_violation THEN NULL;
  END;
END;
$$;

ROLLBACK;
