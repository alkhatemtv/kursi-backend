# Database Migrations

Schema changes go through **Alembic**. Nothing else may change the schema.

- Migration scripts: `alembic/versions/`
- Alembic config: `alembic.ini` (contains **no** connection string)
- Environment wiring: `alembic/env.py`
- Deploy entry point: `scripts/migrate.py`
- Baseline revision: **`d29b3ede11f0`**

The database URL always comes from the `DATABASE_URL` environment variable via
`app.config.settings`. It is never hardcoded, so Alembic and the app can never
disagree about which database they are pointed at.

---

## The baseline revision

The live database was originally built by `Base.metadata.create_all()`, before
Alembic existed. Revision `d29b3ede11f0` reproduces that schema exactly, so
Alembic history starts from what is already deployed.

It was verified two ways:

1. `alembic check` against a database built from the migration →
   *"No new upgrade operations detected"* (empty diff vs. the models).
2. A schema built by `create_all()` and a schema built by `alembic upgrade head`
   were dumped and diffed → **identical across all 24 schema objects**.

> **Never run `alembic upgrade` on a database that already has the tables.**
> It would try to `CREATE TABLE users` on a database that already has one and
> fail the deploy. Such a database gets **stamped** instead — see below.

---

## How migrations are applied on Railway

**Mechanism: a release step inside the start command**, `scripts/start.sh`:

```bash
python -m scripts.migrate        # migrations run first
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
```

Set this as the **Custom Start Command** in the Railway service settings:

```
bash scripts/start.sh
```

### Why this mechanism and not the alternative

The obvious alternative is running `alembic upgrade head` from the FastAPI
`lifespan` hook. That was rejected:

| | start command (chosen) | app lifespan hook |
|---|---|---|
| Runs before traffic is served | Yes — uvicorn only starts after it exits 0 | No — the server is already coming up |
| Failure behaviour | `set -e` fails the deploy; Railway keeps the old version live | App boots against a half-migrated schema, or crash-loops |
| Concurrency | Runs once per deploy | Runs once **per worker/replica** — concurrent DDL on the same database |
| Migration output | Plain, greppable deploy logs | Interleaved with request logs |
| Testability | A plain script you can run by hand | Only reachable by booting the app |

The concurrency point is the decisive one: the moment the service runs more than
one uvicorn worker or replica, a lifespan migration means several processes
racing to run the same DDL.

Keeping migrations out of the app also keeps `app/main.py` honest — its startup
only *checks* the revision (see [Startup safety check](#startup-safety-check)),
it never mutates the schema.

### What `scripts/migrate.py` does

It inspects the database and picks one of three paths:

1. **Empty database** (a fresh staging DB) → `alembic upgrade head`, creating
   everything from the baseline.
2. **Tables exist but no `alembic_version`** — *this is today's production
   database* → `alembic stamp d29b3ede11f0`, then `upgrade head`. Stamping
   writes a single row to `alembic_version`; it does not touch, rewrite, or drop
   any existing table or any data.
3. **Already under Alembic control** → `alembic upgrade head` (a no-op at head).

So the first production deploy after this change adopts the existing database
automatically, with no manual step and no risk to the data.

---

## Workflow: making a schema change

### 1. Create the revision (locally)

Point `DATABASE_URL` at a **local** database that is already at head, then:

```bash
alembic upgrade head                                   # get local DB to head first
alembic revision --autogenerate -m "add events.foo"
```

Autogenerate diffs `app/models.py` against the database, so the local database
must be at head or the diff will be wrong.

### 2. Review it — always

Open the generated file in `alembic/versions/` and check:

- Only the change you intended is present — no accidental `drop_table` /
  `drop_column`. **Autogenerate cannot see your intent**; a model you forgot to
  import looks exactly like a table you meant to delete.
- `downgrade()` actually reverses `upgrade()`.
- Data-destructive operations are deliberate.
- Adding a `NOT NULL` column to a populated table needs a `server_default` or a
  three-step deploy (add nullable → backfill → set not-null).

Confirm the diff is now empty:

```bash
alembic upgrade head
alembic check        # expect: "No new upgrade operations detected."
```

### 3. Apply to staging

Staging is a separate Railway environment with its own Postgres. Deploying there
runs `scripts/start.sh`, which migrates automatically. Verify:

```bash
curl https://<staging-host>/health
# {"status":"ok","env":"staging","migration_state":"up_to_date", ...}
```

### 4. Apply to production

1. **Take a backup first** — see [`scripts/backup.md`](scripts/backup.md).
2. Deploy. `scripts/start.sh` runs the migration before uvicorn binds a port.
3. Watch the deploy log for `Running: alembic upgrade head` and the revision line.
4. Verify:

```bash
curl https://<production-host>/health
# migration_state must be "up_to_date" and db_revision == head_revision
```

If the migration fails, the deploy fails and Railway keeps serving the previous
version — the database is the thing to check, not the app.

---

## Startup safety check

`app/main.py` calls `log_migration_state()` on startup (`app/migrations.py`).
It compares the revision stamped in the database against the head revision on
disk and **logs** the result:

- match → `INFO  Database schema is at head revision <rev>`
- drift → `WARNING  DATABASE SCHEMA OUT OF DATE: database is at revision ... but the code expects ...`
- never stamped → a warning pointing at `alembic stamp head`

It **never raises and never blocks startup**. A schema one revision behind is
usually still serving traffic fine; turning that into a boot crash would convert
a warning into an outage. The same information is exposed on `GET /health` as
`db_revision`, `head_revision`, and `migration_state`.

---

## Command reference

Every command reads `DATABASE_URL` from the environment.

```bash
alembic current                      # revision this database is at
alembic heads                        # revision the code expects
alembic history --verbose            # full history
alembic check                        # models vs database — empty diff?
alembic upgrade head                 # apply everything pending
alembic downgrade -1                 # step back one revision
alembic upgrade head --sql           # print the SQL instead of running it
alembic stamp <rev>                  # record a revision WITHOUT running it

python -m scripts.migrate            # what the deploy runs
```

Pointing Alembic at a one-off database without exporting anything:

```bash
alembic -x db_url=sqlite:///./scratch.db upgrade head
```

### Local sanity check on a throwaway database

```bash
DATABASE_URL=sqlite:///./scratch.db alembic upgrade head
DATABASE_URL=sqlite:///./scratch.db alembic check
rm scratch.db
```
