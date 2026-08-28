# Go Study bot

Go Study is a derivative deployment of the [StudyLion Discord study and productivity bot](https://github.com/StudyLions/StudyLion). It retains StudyLion's camera-gated verified study tracking and adds the Go Study verified hourly reward catalog and inventory. The upstream project was founded by Ari Horesh and developed by the StudyLion contributors.

This repository remains subject to the [StudyLion Open Source License](LICENSE.md). In particular, preserve the original copyright and attribution, do not claim authorship of the upstream work, do not sublicense it, and do not use the software or a derivative commercially. `LICENSE.md` is authoritative.

## Supported local environment

Local development is supported on WSL 2 or Linux with:

- Git with submodule support
- Python 3.11.16
- `uv`
- PostgreSQL
- a Discord bot application and token

The dependency set in `requirements.txt` is a fully pinned Python 3.11 environment. It deliberately uses pure-Python `psycopg==3.1.18` with `psycopg-pool==3.3.1`; do not install `psycopg-binary`. The bot currently relies on `psycopg._encodings.pgconn_encoding`, which is not compatible with newer psycopg releases.

On Ubuntu/WSL, install the system prerequisites first:

```bash
sudo apt update
sudo apt install -y build-essential curl git libffi-dev libjpeg-dev postgresql zlib1g-dev
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Fresh installation

Clone recursively. If the repository was already cloned without submodules, initialize them before continuing.

```bash
git clone --recursive https://github.com/usingbot/gostudy StudyLion
cd StudyLion
git submodule update --init --recursive
```

Create the exact Python environment and synchronize it to the lock-style requirements file:

```bash
uv python install 3.11.16
uv venv --python 3.11.16 .venv
uv pip sync requirements.txt
```

### Create a fresh database

Create a dedicated PostgreSQL role and database. Enter the role password interactively; do not put it in shell history or source control.

```bash
sudo -u postgres createuser --pwprompt gostudy
sudo -u postgres createdb --owner=gostudy gostudy
```

For a new, empty database, load `data/schema.sql` exactly once. It already represents schema version 16, so do not replay any historical migration after it.

```bash
psql "dbname=gostudy user=gostudy host=127.0.0.1" \
  --set ON_ERROR_STOP=on \
  --single-transaction \
  --file data/schema.sql

psql "dbname=gostudy user=gostudy host=127.0.0.1" \
  --tuples-only --no-align \
  --command 'SELECT version FROM VersionHistory ORDER BY time DESC LIMIT 1;'
```

The verification command must print `16`.

### Configure local runtime files

Copy the tracked templates. These runtime copies are ignored by Git and must never be committed.

```bash
cp config/example-bot.conf config/bot.conf
cp config/example-secrets.conf config/secrets.conf
cp config/example-gui.conf config/gui.conf
chmod 600 config/bot.conf config/secrets.conf config/gui.conf
```

Replace the placeholders in `config/secrets.conf` with the Discord bot token and PostgreSQL connection settings. Keep the token and database password only in that ignored file. In the Discord Developer Portal, enable the Server Members and Message Content privileged gateway intents used by this bot.

The local template intentionally defaults these optional integrations off:

- `ANALYTICS.enabled = false`: the analytics extension and worker are not required locally.
- `PREMIUM.enabled = false`: legacy gems, premium commands, and the gem webhook are not required by Go Study.
- `TOPGG.enabled = false`: Top.gg posting and its webhook remain inactive.

Blank or malformed optional premium webhook configuration is ignored safely. Default skins and statistics continue to work without Premium; user skin purchasing and premium server branding are unavailable while it is disabled.

## Validate and run

Run the preflight from the repository root. It checks Python and dependency versions, the psycopg compatibility import, submodules, configuration syntax, required runtime settings, local ports, PostgreSQL connectivity, and schema version. It reports only status—not tokens, passwords, connection strings, or configuration values.

```bash
.venv/bin/python scripts/preflight.py --config config/bot.conf
```

Then start the registry and bot together:

```bash
.venv/bin/python scripts/dev.py --config config/bot.conf
```

The supervisor runs from the repository root internally, waits for the registry before starting the bot, inherits both processes' output, and never applies database migrations. Press Ctrl-C once to stop. It asks the bot to shut down first, allowing Discord, HTTP, database, and IPC resources to close, and then stops the registry. Both stages have bounded termination fallbacks.

The lower-level entry points remain available for debugging, but they normally should not be needed:

```bash
.venv/bin/python scripts/start_registry.py --conf config/bot.conf
.venv/bin/python scripts/start_leo.py --conf config/bot.conf --shard 0 --host 127.0.0.1 --port 5001
```

The development supervisor does not start the analytics worker or GUI service. Only enable and launch an optional legacy service when actively developing it. Premium is an in-process module rather than a separate service; leave it disabled for Go Study.

## Updating an existing database

Never run `data/schema.sql` against an existing database and never replay the full migration history. Before changing an existing database:

1. Stop writers and create a restorable backup.
2. Query `VersionHistory` and confirm the current version.
3. Review and apply only the single migration from that version to the next version.
4. Use `ON_ERROR_STOP` and let the migration's own transaction control roll back failures.
5. Re-query `VersionHistory` after every step.

Example backup and version checks:

```bash
pg_dump -Fc --file gostudy-before-migration.dump \
  "dbname=gostudy user=gostudy host=127.0.0.1"

psql "dbname=gostudy user=gostudy host=127.0.0.1" \
  --command 'SELECT version, time, author FROM VersionHistory ORDER BY time DESC LIMIT 1;'
```

If and only if that query reports version 15 and the reviewed target is version 16:

```bash
psql "dbname=gostudy user=gostudy host=127.0.0.1" \
  --set ON_ERROR_STOP=on \
  --file data/migration/v15-v16/migration.sql
```

Migrations are not idempotent. The launcher and preflight intentionally never modify the database.

## Verification and troubleshooting

Run the repository's hardening checks without connecting a Discord bot:

```bash
.venv/bin/python -m compileall -q src scripts tests
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Common preflight failures:

- **Wrong Python or packages:** recreate `.venv` with the `uv` commands above. Do not upgrade psycopg independently.
- **Missing submodules:** run `git submodule update --init --recursive`.
- **Invalid configuration:** recopy the examples and reapply local values. ConfigParser interpolation references must end in `s`, such as `%(general_log)s`.
- **Port already in use:** stop an existing registry/bot process or choose a different bot IPC port; the registry and bot ports must differ.
- **Database unavailable:** start PostgreSQL and check the ignored local connection configuration without pasting it into logs or issue reports.
- **Schema mismatch:** inspect `VersionHistory`, restore from backup if necessary, and apply only the appropriate next migration. Do not let the launcher migrate automatically.

## Current Go Study behavior

The bot includes Discord login, IPC registry integration, camera-gated verified study tracking, verified hourly rewards, a reward catalog, and per-user inventory on PostgreSQL schema version 16. Hardening does not change study verification or reward behavior; it only makes local installation, optional-module defaults, startup validation, process supervision, and shutdown reproducible.
