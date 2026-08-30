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

    IF _current_version IS DISTINCT FROM 21 THEN
      RAISE EXCEPTION
        'Migration v21-v22 requires schema version 21; found %.',
        COALESCE(_current_version::TEXT, 'NULL');
    END IF;
  END;
$$ LANGUAGE PLPGSQL;

-- Go Study Discord guild registry runtime privileges {{{
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE
  public.gostudy_guilds,
  public.gostudy_guild_emojis,
  public.gostudy_guild_stickers
FROM PUBLIC;

REVOKE DELETE, TRUNCATE ON TABLE
  public.gostudy_guilds,
  public.gostudy_guild_emojis,
  public.gostudy_guild_stickers
FROM lion;

GRANT SELECT, INSERT, UPDATE ON TABLE
  public.gostudy_guilds,
  public.gostudy_guild_emojis,
  public.gostudy_guild_stickers
TO lion;
-- }}}

INSERT INTO public.VersionHistory (version, author)
VALUES (22, 'v21-v22 migration');

COMMIT;
