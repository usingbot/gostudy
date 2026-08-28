BEGIN;

CREATE TABLE gostudy_reward_catalog(
  catalog_itemid SERIAL PRIMARY KEY,
  item_key TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  description TEXT,
  asset_key TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::JSONB
    CHECK (jsonb_typeof(metadata) = 'object'),
  selection_order INTEGER NOT NULL UNIQUE CHECK (selection_order > 0),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE gostudy_user_inventory(
  hour_rewardid BIGINT PRIMARY KEY
    REFERENCES gostudy_hour_rewards (rewardid) ON DELETE RESTRICT,
  catalog_itemid INTEGER NOT NULL
    REFERENCES gostudy_reward_catalog (catalog_itemid) ON DELETE RESTRICT,
  granted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO gostudy_reward_catalog (
  item_key, display_name, description, asset_key, metadata, selection_order
) VALUES
  (
    'verified-hour-token',
    'Verified Hour Token',
    'Permanent fallback reward for one verified study hour.',
    'rewards/verified-hour-token',
    '{"category": "fallback"}'::JSONB,
    1
  ),
  (
    'coffee',
    'Coffee',
    'A coffee for the next focused study session.',
    'rewards/coffee',
    '{"category": "mvp"}'::JSONB,
    2
  ),
  (
    'books',
    'Books',
    'A stack of books earned through verified study.',
    'rewards/books',
    '{"category": "mvp"}'::JSONB,
    3
  ),
  (
    'moon',
    'Moon',
    'A moon for a completed verified study hour.',
    'rewards/moon',
    '{"category": "mvp"}'::JSONB,
    4
  ),
  (
    'study-star',
    'Study Star',
    'A star earned through verified study.',
    'rewards/study-star',
    '{"category": "mvp"}'::JSONB,
    5
  );

CREATE FUNCTION gostudy_protect_fallback_catalog_item()
  RETURNS TRIGGER
AS $$
  BEGIN
    IF TG_OP = 'TRUNCATE' THEN
      RAISE EXCEPTION 'The Go Study catalog cannot be truncated because it contains the permanent fallback item.';
    END IF;

    IF TG_OP = 'DELETE' THEN
      IF OLD.item_key = 'verified-hour-token' THEN
        RAISE EXCEPTION 'The permanent Go Study fallback catalog item cannot be deleted.';
      END IF;
      RETURN OLD;
    END IF;

    IF OLD.item_key = 'verified-hour-token' AND (
      NEW.catalog_itemid IS DISTINCT FROM OLD.catalog_itemid
      OR NEW.item_key IS DISTINCT FROM OLD.item_key
      OR NEW.active IS NOT TRUE
    ) THEN
      RAISE EXCEPTION 'The permanent Go Study fallback catalog item cannot be disabled or reidentified.';
    END IF;

    RETURN NEW;
  END;
$$ LANGUAGE PLPGSQL;

CREATE TRIGGER gostudy_protect_fallback_catalog_item
BEFORE UPDATE OR DELETE ON gostudy_reward_catalog
FOR EACH ROW EXECUTE FUNCTION gostudy_protect_fallback_catalog_item();

CREATE TRIGGER gostudy_protect_fallback_catalog_truncate
BEFORE TRUNCATE ON gostudy_reward_catalog
FOR EACH STATEMENT EXECUTE FUNCTION gostudy_protect_fallback_catalog_item();

CREATE FUNCTION gostudy_grant_hour_reward(_rewardid BIGINT)
  RETURNS INTEGER
AS $$
  DECLARE
    _milestone_hour BIGINT;
    _catalog_itemid INTEGER;
    _items_granted INTEGER;
  BEGIN
    -- Replays, including replays after catalog changes, never reroll an item.
    IF EXISTS (
      SELECT 1
      FROM gostudy_user_inventory
      WHERE hour_rewardid = _rewardid
    ) THEN
      RETURN 0;
    END IF;

    SELECT milestone_hour INTO _milestone_hour
    FROM gostudy_hour_rewards
    WHERE rewardid = _rewardid;

    IF NOT FOUND THEN
      RETURN 0;
    END IF;

    -- Select from one statement snapshot so concurrent catalog maintenance
    -- cannot make the count and selected row disagree.
    WITH active_items AS (
      SELECT
        catalog_itemid,
        row_number() OVER (ORDER BY selection_order, catalog_itemid) - 1 AS item_slot,
        count(*) OVER () AS item_count
      FROM gostudy_reward_catalog
      WHERE active AND item_key <> 'verified-hour-token'
    )
    SELECT catalog_itemid INTO _catalog_itemid
    FROM active_items
    WHERE item_slot = mod(_milestone_hour - 1, item_count);

    -- The protected fallback makes an empty normal selection pool harmless.
    IF _catalog_itemid IS NULL THEN
      SELECT catalog_itemid INTO _catalog_itemid
      FROM gostudy_reward_catalog
      WHERE item_key = 'verified-hour-token';
    END IF;

    INSERT INTO gostudy_user_inventory (hour_rewardid, catalog_itemid)
    VALUES (_rewardid, _catalog_itemid)
    ON CONFLICT (hour_rewardid) DO NOTHING;

    GET DIAGNOSTICS _items_granted = ROW_COUNT;
    RETURN _items_granted;
  END;
$$ LANGUAGE PLPGSQL;

CREATE FUNCTION gostudy_grant_new_hour_reward()
  RETURNS TRIGGER
AS $$
  BEGIN
    PERFORM gostudy_grant_hour_reward(NEW.rewardid);
    RETURN NEW;
  END;
$$ LANGUAGE PLPGSQL;

-- Installed after the catalog is seeded. Existing hour rewards are
-- intentionally not scanned or granted by this migration.
CREATE TRIGGER gostudy_grant_new_hour_reward
AFTER INSERT ON gostudy_hour_rewards
FOR EACH ROW EXECUTE FUNCTION gostudy_grant_new_hour_reward();

INSERT INTO VersionHistory (version, author)
VALUES (16, 'v15-v16 migration');

COMMIT;
