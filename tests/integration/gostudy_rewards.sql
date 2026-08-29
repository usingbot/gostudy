-- Run with psql against a disposable database after applying schema v18.
-- Every write is rolled back.
BEGIN;

INSERT INTO user_config (userid) VALUES
  (-15001), (-15002), (-15003), (-15004);
INSERT INTO guild_config (guildid) VALUES (-15000);
INSERT INTO members (guildid, userid) VALUES
  (-15000, -15001),
  (-15000, -15002),
  (-15000, -15003),
  (-15000, -15004);

-- Segmented accumulation: 3599 -> 3601 -> 7199 -> 7201.
INSERT INTO voice_sessions (
  guildid, userid, start_time, duration, video_duration
) VALUES (-15000, -15001, now(), 3599, 3599);

DO $$
BEGIN
  IF (SELECT verified_seconds FROM gostudy_reward_accounts WHERE userid = -15001) <> 3599
     OR (SELECT COUNT(*) FROM gostudy_hour_rewards WHERE userid = -15001) <> 0 THEN
    RAISE EXCEPTION '3599-second reward assertion failed';
  END IF;
END;
$$;

INSERT INTO voice_sessions (
  guildid, userid, start_time, duration, video_duration
) VALUES (-15000, -15001, now(), 2, 2);

DO $$
BEGIN
  IF (SELECT verified_seconds FROM gostudy_reward_accounts WHERE userid = -15001) <> 3601
     OR (SELECT COUNT(*) FROM gostudy_hour_rewards WHERE userid = -15001) <> 1 THEN
    RAISE EXCEPTION '3601-second reward assertion failed';
  END IF;
END;
$$;

INSERT INTO voice_sessions (
  guildid, userid, start_time, duration, video_duration
) VALUES (-15000, -15001, now(), 3598, 3598);

DO $$
BEGIN
  IF (SELECT verified_seconds FROM gostudy_reward_accounts WHERE userid = -15001) <> 7199
     OR (SELECT COUNT(*) FROM gostudy_hour_rewards WHERE userid = -15001) <> 1 THEN
    RAISE EXCEPTION '7199-second reward assertion failed';
  END IF;
END;
$$;

INSERT INTO voice_sessions (
  guildid, userid, start_time, duration, video_duration
) VALUES (-15000, -15001, now(), 2, 2);

DO $$
BEGIN
  IF (SELECT verified_seconds FROM gostudy_reward_accounts WHERE userid = -15001) <> 7201
     OR (SELECT COUNT(*) FROM gostudy_hour_rewards WHERE userid = -15001) <> 2 THEN
    RAISE EXCEPTION '7201-second reward assertion failed';
  END IF;
END;
$$;

-- Exact threshold and replay idempotency.
INSERT INTO voice_sessions (
  guildid, userid, start_time, duration, video_duration, tag
) VALUES (-15000, -15002, now(), 3600, 3600, 'gostudy-reward-replay-test');

SELECT gostudy_process_voice_session(sessionid)
FROM voice_sessions
WHERE guildid = -15000 AND userid = -15002;

DO $$
BEGIN
  IF (SELECT verified_seconds FROM gostudy_reward_accounts WHERE userid = -15002) <> 3600
     OR (SELECT COUNT(*) FROM gostudy_hour_rewards WHERE userid = -15002) <> 1
     OR (SELECT COUNT(*) FROM gostudy_verified_session_credits WHERE userid = -15002) <> 1 THEN
    RAISE EXCEPTION 'exact-threshold or replay-idempotency assertion failed';
  END IF;
END;
$$;

-- A single completed segment can cross more than one milestone.
INSERT INTO voice_sessions (
  guildid, userid, start_time, duration, video_duration
) VALUES (-15000, -15003, now(), 7201, 7201);

DO $$
BEGIN
  IF (SELECT COUNT(*) FROM gostudy_hour_rewards WHERE userid = -15003) <> 2
     OR (SELECT MIN(milestone_hour) FROM gostudy_hour_rewards WHERE userid = -15003) <> 1
     OR (SELECT MAX(milestone_hour) FROM gostudy_hour_rewards WHERE userid = -15003) <> 2 THEN
    RAISE EXCEPTION 'multiple-milestone assertion failed';
  END IF;
END;
$$;

-- Only camera-verified seconds can be credited, never the larger wall duration.
INSERT INTO voice_sessions (
  guildid, userid, start_time, duration, video_duration
) VALUES (-15000, -15004, now(), 7201, 3599);

DO $$
BEGIN
  IF (SELECT verified_seconds FROM gostudy_reward_accounts WHERE userid = -15004) <> 3599
     OR (SELECT COUNT(*) FROM gostudy_hour_rewards WHERE userid = -15004) <> 0 THEN
    RAISE EXCEPTION 'verified-duration cap assertion failed';
  END IF;
END;
$$;

-- Every hour reward created through verified-session processing receives one
-- inventory item and one notification outbox row through the post-v16/v17
-- reward triggers.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM gostudy_hour_rewards AS rewards
    LEFT JOIN gostudy_user_inventory AS inventory
      ON inventory.hour_rewardid = rewards.rewardid
    WHERE
      rewards.userid IN (-15001, -15002, -15003, -15004)
      AND inventory.hour_rewardid IS NULL
  ) THEN
    RAISE EXCEPTION 'verified-session to inventory assertion failed';
  END IF;
END;
$$;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM gostudy_hour_rewards AS rewards
    LEFT JOIN gostudy_reward_notifications AS notifications
      ON notifications.hour_rewardid = rewards.rewardid
    WHERE
      rewards.userid IN (-15001, -15002, -15003, -15004)
      AND notifications.notificationid IS NULL
  ) THEN
    RAISE EXCEPTION 'verified-session to notification assertion failed';
  END IF;
END;
$$;

ROLLBACK;
