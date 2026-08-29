-- Run with psql against a disposable database after applying schema v20.
-- Every write is rolled back.
BEGIN;

DO $$
BEGIN
  IF (
    SELECT array_agg(item_key ORDER BY selection_order, catalog_itemid)
    FROM gostudy_reward_catalog
  ) <> ARRAY[
    'verified-hour-token', 'coffee', 'books', 'moon', 'study-star'
  ] THEN
    RAISE EXCEPTION 'MVP catalog seed assertion failed';
  END IF;
END;
$$;

INSERT INTO gostudy_verified_session_credits (
  source_sessionid, userid, source_guildid, verified_seconds
) VALUES
  (-16001, -16000, -16000, 3600),
  (-16002, -16000, -16000, 3600),
  (-16003, -16000, -16000, 3600),
  (-16004, -16000, -16000, 3600),
  (-16005, -16000, -16000, 3600),
  (-16006, -16000, -16000, 3600),
  (-16007, -16001, -16000, 3600);

-- New rewards grant automatically and rotate over the normal active pool.
INSERT INTO gostudy_hour_rewards (
  userid, milestone_hour, source_sessionid, verified_seconds_at_award
) VALUES
  (-16000, 1, -16001, 3600),
  (-16000, 2, -16002, 7200),
  (-16000, 3, -16003, 10800),
  (-16000, 4, -16004, 14400),
  (-16000, 5, -16005, 18000);

DO $$
BEGIN
  IF (
    SELECT array_agg(catalog.item_key ORDER BY rewards.milestone_hour)
    FROM gostudy_user_inventory AS inventory
    JOIN gostudy_hour_rewards AS rewards
      ON rewards.rewardid = inventory.hour_rewardid
    JOIN gostudy_reward_catalog AS catalog
      ON catalog.catalog_itemid = inventory.catalog_itemid
    WHERE rewards.userid = -16000
  ) <> ARRAY['coffee', 'books', 'moon', 'study-star', 'coffee'] THEN
    RAISE EXCEPTION 'deterministic round-robin assertion failed';
  END IF;

  IF (
    SELECT COUNT(*)
    FROM gostudy_user_inventory AS inventory
    JOIN gostudy_hour_rewards AS rewards
      ON rewards.rewardid = inventory.hour_rewardid
    WHERE rewards.userid = -16000
  ) <> 5 THEN
    RAISE EXCEPTION 'one-item-per-hour-reward assertion failed';
  END IF;

  IF (
    SELECT COUNT(*)
    FROM gostudy_user_inventory AS inventory
    JOIN gostudy_hour_rewards AS rewards
      ON rewards.rewardid = inventory.hour_rewardid
    JOIN gostudy_reward_catalog AS catalog
      ON catalog.catalog_itemid = inventory.catalog_itemid
    WHERE rewards.userid = -16000 AND catalog.item_key = 'coffee'
  ) <> 2 THEN
    RAISE EXCEPTION 'duplicate catalog item assertion failed';
  END IF;
END;
$$;

-- Reprocessing is idempotent.
SELECT gostudy_grant_hour_reward(rewardid)
FROM gostudy_hour_rewards
WHERE userid = -16000;

DO $$
BEGIN
  IF (
    SELECT COUNT(*)
    FROM gostudy_user_inventory AS inventory
    JOIN gostudy_hour_rewards AS rewards
      ON rewards.rewardid = inventory.hour_rewardid
    WHERE rewards.userid = -16000
  ) <> 5 THEN
    RAISE EXCEPTION 'replay idempotency assertion failed';
  END IF;
END;
$$;

-- Catalog changes never reroll an inventory item that was already granted.
UPDATE gostudy_reward_catalog
SET active = FALSE
WHERE item_key = 'coffee';

SELECT gostudy_grant_hour_reward(rewardid)
FROM gostudy_hour_rewards
WHERE userid = -16000 AND milestone_hour = 1;

DO $$
BEGIN
  IF (
    SELECT catalog.item_key
    FROM gostudy_user_inventory AS inventory
    JOIN gostudy_hour_rewards AS rewards
      ON rewards.rewardid = inventory.hour_rewardid
    JOIN gostudy_reward_catalog AS catalog
      ON catalog.catalog_itemid = inventory.catalog_itemid
    WHERE rewards.userid = -16000 AND rewards.milestone_hour = 1
  ) <> 'coffee' THEN
    RAISE EXCEPTION 'granted item reroll protection assertion failed';
  END IF;
END;
$$;

-- With no normal active items, new rewards receive the permanent fallback.
UPDATE gostudy_reward_catalog
SET active = FALSE
WHERE item_key <> 'verified-hour-token';

INSERT INTO gostudy_hour_rewards (
  userid, milestone_hour, source_sessionid, verified_seconds_at_award
) VALUES (-16000, 6, -16006, 21600);

DO $$
BEGIN
  IF (
    SELECT catalog.item_key
    FROM gostudy_user_inventory AS inventory
    JOIN gostudy_hour_rewards AS rewards
      ON rewards.rewardid = inventory.hour_rewardid
    JOIN gostudy_reward_catalog AS catalog
      ON catalog.catalog_itemid = inventory.catalog_itemid
    WHERE rewards.userid = -16000 AND rewards.milestone_hour = 6
  ) <> 'verified-hour-token' THEN
    RAISE EXCEPTION 'fallback selection assertion failed';
  END IF;
END;
$$;

-- The fallback cannot be disabled or deleted accidentally.
DO $$
DECLARE
  update_blocked BOOLEAN := FALSE;
  delete_blocked BOOLEAN := FALSE;
BEGIN
  BEGIN
    UPDATE gostudy_reward_catalog
    SET active = FALSE
    WHERE item_key = 'verified-hour-token';
  EXCEPTION WHEN raise_exception THEN
    update_blocked := TRUE;
  END;

  BEGIN
    DELETE FROM gostudy_reward_catalog
    WHERE item_key = 'verified-hour-token';
  EXCEPTION WHEN raise_exception THEN
    delete_blocked := TRUE;
  END;

  IF NOT update_blocked OR NOT delete_blocked THEN
    RAISE EXCEPTION 'fallback catalog protection assertion failed';
  END IF;
END;
$$;

-- Simulate a reward that predates trigger installation: it stays unassigned
-- until explicitly processed, then remains idempotent.
ALTER TABLE gostudy_hour_rewards
  DISABLE TRIGGER gostudy_grant_new_hour_reward;

INSERT INTO gostudy_hour_rewards (
  userid, milestone_hour, source_sessionid, verified_seconds_at_award
) VALUES (-16001, 1, -16007, 3600);

ALTER TABLE gostudy_hour_rewards
  ENABLE TRIGGER gostudy_grant_new_hour_reward;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM gostudy_user_inventory AS inventory
    JOIN gostudy_hour_rewards AS rewards
      ON rewards.rewardid = inventory.hour_rewardid
    WHERE rewards.userid = -16001
  ) THEN
    RAISE EXCEPTION 'historical no-backfill assertion failed';
  END IF;
END;
$$;

SELECT gostudy_grant_hour_reward(rewardid)
FROM gostudy_hour_rewards
WHERE userid = -16001;

SELECT gostudy_grant_hour_reward(rewardid)
FROM gostudy_hour_rewards
WHERE userid = -16001;

DO $$
BEGIN
  IF (
    SELECT COUNT(*)
    FROM gostudy_user_inventory AS inventory
    JOIN gostudy_hour_rewards AS rewards
      ON rewards.rewardid = inventory.hour_rewardid
    WHERE rewards.userid = -16001
  ) <> 1 THEN
    RAISE EXCEPTION 'explicit historical processing assertion failed';
  END IF;
END;
$$;

ROLLBACK;
