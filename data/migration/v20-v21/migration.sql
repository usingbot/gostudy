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

    IF _current_version IS DISTINCT FROM 20 THEN
      RAISE EXCEPTION
        'Migration v20-v21 requires schema version 20; found %.',
        COALESCE(_current_version::TEXT, 'NULL');
    END IF;
  END;
$$ LANGUAGE PLPGSQL;

-- Go Study Discord guild registry {{{
CREATE TABLE public.gostudy_guilds(
  guildid BIGINT PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  icon_hash VARCHAR(128),
  banner_hash VARCHAR(128),
  description VARCHAR(120),
  member_count INTEGER,
  active BOOLEAN NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_synced_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT gostudy_guilds_guildid_positive
    CHECK (guildid > 0),
  CONSTRAINT gostudy_guilds_name_bounded
    CHECK (char_length(name) BETWEEN 1 AND 100),
  CONSTRAINT gostudy_guilds_icon_hash_format
    CHECK (
      icon_hash IS NULL
      OR icon_hash ~ '^(a_)?[0-9a-f]{1,128}$'
    ),
  CONSTRAINT gostudy_guilds_banner_hash_format
    CHECK (
      banner_hash IS NULL
      OR banner_hash ~ '^(a_)?[0-9a-f]{1,128}$'
    ),
  CONSTRAINT gostudy_guilds_description_bounded
    CHECK (description IS NULL OR char_length(description) <= 120),
  CONSTRAINT gostudy_guilds_member_count_nonnegative
    CHECK (member_count IS NULL OR member_count >= 0),
  CONSTRAINT gostudy_guilds_timestamp_order
    CHECK (
      first_seen_at <= last_synced_at
      AND updated_at >= first_seen_at
    )
);

CREATE TABLE public.gostudy_guild_emojis(
  emojiid BIGINT PRIMARY KEY,
  guildid BIGINT NOT NULL
    REFERENCES public.gostudy_guilds (guildid) ON DELETE CASCADE,
  name VARCHAR(100) NOT NULL,
  animated BOOLEAN NOT NULL,
  available BOOLEAN NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT gostudy_guild_emojis_emojiid_positive
    CHECK (emojiid > 0),
  CONSTRAINT gostudy_guild_emojis_guildid_positive
    CHECK (guildid > 0),
  CONSTRAINT gostudy_guild_emojis_name_bounded
    CHECK (char_length(name) BETWEEN 1 AND 100),
  CONSTRAINT gostudy_guild_emojis_timestamp_order
    CHECK (
      first_seen_at <= last_seen_at
      AND updated_at >= first_seen_at
    )
);
CREATE INDEX gostudy_guild_emojis_guilds
  ON public.gostudy_guild_emojis (guildid, emojiid);

CREATE TABLE public.gostudy_guild_stickers(
  stickerid BIGINT PRIMARY KEY,
  guildid BIGINT NOT NULL
    REFERENCES public.gostudy_guilds (guildid) ON DELETE CASCADE,
  name VARCHAR(100) NOT NULL,
  description VARCHAR(1000),
  format_type SMALLINT NOT NULL,
  sticker_type SMALLINT NOT NULL,
  available BOOLEAN NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT gostudy_guild_stickers_stickerid_positive
    CHECK (stickerid > 0),
  CONSTRAINT gostudy_guild_stickers_guildid_positive
    CHECK (guildid > 0),
  CONSTRAINT gostudy_guild_stickers_name_bounded
    CHECK (char_length(name) BETWEEN 1 AND 100),
  CONSTRAINT gostudy_guild_stickers_description_bounded
    CHECK (description IS NULL OR char_length(description) <= 1000),
  CONSTRAINT gostudy_guild_stickers_format_type_bounded
    CHECK (format_type BETWEEN 0 AND 32767),
  CONSTRAINT gostudy_guild_stickers_sticker_type_bounded
    CHECK (sticker_type BETWEEN 0 AND 32767),
  CONSTRAINT gostudy_guild_stickers_timestamp_order
    CHECK (
      first_seen_at <= last_seen_at
      AND updated_at >= first_seen_at
    )
);
CREATE INDEX gostudy_guild_stickers_guilds
  ON public.gostudy_guild_stickers (guildid, stickerid);
-- }}}

INSERT INTO public.VersionHistory (version, author)
VALUES (21, 'v20-v21 migration');

COMMIT;
