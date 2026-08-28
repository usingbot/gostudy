BEGIN;

-- One row per completed voice session processed by Go Study rewards.
-- Creating this table empty is intentional: historical sessions are not backfilled.
CREATE TABLE gostudy_verified_session_credits(
  source_sessionid INTEGER PRIMARY KEY,
  userid BIGINT NOT NULL,
  source_guildid BIGINT NOT NULL,
  verified_seconds BIGINT NOT NULL CHECK (verified_seconds >= 0),
  processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX gostudy_verified_session_credits_users
  ON gostudy_verified_session_credits (userid, source_sessionid);

-- Global cumulative verified time for each Discord user.
CREATE TABLE gostudy_reward_accounts(
  userid BIGINT PRIMARY KEY,
  verified_seconds BIGINT NOT NULL DEFAULT 0 CHECK (verified_seconds >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Each row is one earned Go Study reward. The unique milestone is the
-- persistent idempotency key exposed to future web/API consumers.
CREATE TABLE gostudy_hour_rewards(
  rewardid BIGSERIAL PRIMARY KEY,
  userid BIGINT NOT NULL,
  milestone_hour BIGINT NOT NULL CHECK (milestone_hour > 0),
  source_sessionid INTEGER NOT NULL
    REFERENCES gostudy_verified_session_credits (source_sessionid),
  verified_seconds_at_award BIGINT NOT NULL CHECK (verified_seconds_at_award >= 3600),
  earned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (userid, milestone_hour)
);
CREATE INDEX gostudy_hour_rewards_users
  ON gostudy_hour_rewards (userid, rewardid);

CREATE FUNCTION gostudy_process_voice_session(_sessionid INTEGER)
  RETURNS INTEGER
AS $$
  DECLARE
    _userid BIGINT;
    _credit BIGINT;
    _old_total BIGINT;
    _new_total BIGINT;
    _rewards_created INTEGER;
  BEGIN
    -- The source-session primary key makes processing/reprocessing idempotent.
    -- video_duration is the camera-verified portion of the completed session;
    -- LEAST also prevents malformed rows from crediting more than duration.
    INSERT INTO gostudy_verified_session_credits (
      source_sessionid, userid, source_guildid, verified_seconds
    )
    SELECT
      sessionid,
      userid,
      guildid,
      GREATEST(0, LEAST(duration, COALESCE(video_duration, 0)))::BIGINT
    FROM voice_sessions
    WHERE sessionid = _sessionid
    ON CONFLICT (source_sessionid) DO NOTHING
    RETURNING userid, verified_seconds INTO _userid, _credit;

    IF NOT FOUND THEN
      RETURN 0;
    END IF;

    -- PostgreSQL serializes conflicting upserts on this user row. Distinct
    -- sessions for the same user therefore receive non-overlapping ranges.
    INSERT INTO gostudy_reward_accounts (userid, verified_seconds)
    VALUES (_userid, _credit)
    ON CONFLICT (userid) DO UPDATE
    SET
      verified_seconds = gostudy_reward_accounts.verified_seconds
        + EXCLUDED.verified_seconds,
      updated_at = now()
    RETURNING verified_seconds INTO _new_total;

    _old_total := _new_total - _credit;

    WITH inserted_rewards AS (
      INSERT INTO gostudy_hour_rewards (
        userid, milestone_hour, source_sessionid, verified_seconds_at_award
      )
      SELECT
        _userid,
        milestone_hour,
        _sessionid,
        _new_total
      FROM generate_series(
        (_old_total / 3600) + 1,
        _new_total / 3600
      ) AS milestones(milestone_hour)
      ON CONFLICT (userid, milestone_hour) DO NOTHING
      RETURNING rewardid
    )
    SELECT COUNT(*)::INTEGER INTO _rewards_created
    FROM inserted_rewards;

    RETURN _rewards_created;
  END;
$$ LANGUAGE PLPGSQL;

CREATE FUNCTION gostudy_process_new_voice_session()
  RETURNS TRIGGER
AS $$
  BEGIN
    PERFORM gostudy_process_voice_session(NEW.sessionid);
    RETURN NEW;
  END;
$$ LANGUAGE PLPGSQL;

-- Installed only after the empty ledgers exist, so only subsequent inserts
-- are processed. No migration statement scans or credits historical rows.
CREATE TRIGGER gostudy_process_new_voice_session
AFTER INSERT ON voice_sessions
FOR EACH ROW EXECUTE FUNCTION gostudy_process_new_voice_session();

INSERT INTO VersionHistory (version, author)
VALUES (15, 'v14-v15 migration');

COMMIT;
