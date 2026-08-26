# Manual backups of the Railway Postgres database

How to take an ad-hoc `pg_dump` of the production database from this machine.

**Automated daily backups are a Railway dashboard toggle, not code** — see
[Automated backups](#automated-backups-dashboard-toggle) at the bottom. This
document covers the *manual* dump only.

---

## What was actually verified from this machine

Everything in this section was checked on **2026-08-26** on the development
machine. **No command in this phase connected to the production database.**

| Fact | Status |
|---|---|
| Railway CLI installed | ✅ `railway 4.61.1` |
| CLI authenticated | ✅ logged in as the project owner |
| This directory linked to a project | ✅ project `proud-energy` |
| Services in the project | ✅ `Postgres`, `kursi-backend`, `postgres-volume`; environment `production` |
| `DATABASE_URL` on the Postgres service | ✅ exists — host is `*.railway.internal:5432` |
| `DATABASE_PUBLIC_URL` on the Postgres service | ⚠️ exists **but currently has no host or port** |
| `RAILWAY_TCP_PROXY_DOMAIN` / `RAILWAY_TCP_PROXY_PORT` | ❌ **absent** |
| `pg_dump` / `psql` on this machine | ❌ **not installed** |
| `docker` on this machine | ❌ not installed |

### The two findings that matter

**1. `DATABASE_URL` is the private-network address.** It resolves to
`postgresql://…@<something>.railway.internal:5432/railway`. The
`.railway.internal` domain only resolves *inside* Railway's network. You cannot
`pg_dump` against it from here, and that has nothing to do with your ISP.

**2. The public TCP proxy is not currently enabled.** `DATABASE_PUBLIC_URL`
exists, but with credentials masked it currently reads:

```
postgresql://postgres:<REDACTED>@:/railway
                                 ↑↑
                    host and port are both empty
```

It is a template referencing `RAILWAY_TCP_PROXY_DOMAIN` and
`RAILWAY_TCP_PROXY_PORT`, and **neither variable exists on the service**. So
there is presently **no public endpoint to dump from**. Route A below starts by
enabling it.

### About the port-22 / ISP block

Your ISP blocking port 22 is **irrelevant to the Postgres proxy route**. Railway's
TCP proxy assigns a high, random port (typically in the 10000–65535 range) and
speaks the **PostgreSQL wire protocol** — it is not SSH and never uses port 22.
Once the proxy is enabled, the port it hands you is the one to use.

### Explicitly NOT verified

Stated plainly rather than guessed:

- **No dump was performed.** Nothing here has been run end to end against the
  live database.
- **Route B (`railway ssh`) is untested from this network.** The connectivity
  check was blocked by this environment's sandbox before it ran, so I cannot
  confirm whether the CLI's `ssh` uses Railway's HTTPS transport (which the ISP
  block would not affect) or a real port-22 SSH connection (which it would).
  Treat Route B as plausible-but-unconfirmed until you run step 1 of it yourself.
- The exact proxy hostname and port are unknown here because the proxy is off.

---

## Prerequisite for Routes A and B: PostgreSQL client tools

`pg_dump` is not installed on this machine. Install the client tools — you do
**not** need a local Postgres server:

```powershell
winget install PostgreSQL.PostgreSQL.17
```

Then add `C:\Program Files\PostgreSQL\17\bin` to `PATH` and confirm:

```powershell
pg_dump --version
```

> **Version rule:** your `pg_dump` must be the **same major version or newer**
> than the server. An older `pg_dump` refuses to dump a newer server. Check the
> server version first with `SELECT version();` (Route B step 2, or the
> dashboard's Postgres service → Data tab).

---

## Route A — public TCP proxy (recommended once enabled)

### 1. Enable the proxy (one-time, dashboard)

Railway dashboard → project **proud-energy** → **Postgres** service →
**Settings** → **Networking** → **Public Networking** → **TCP Proxy**, and
expose the internal port **5432**.

Railway then populates `RAILWAY_TCP_PROXY_DOMAIN` and `RAILWAY_TCP_PROXY_PORT`,
which makes `DATABASE_PUBLIC_URL` resolve to a real host and port.

### 2. Pull the URL from the environment — never hardcode it

The connection string carries the production password. Read it from Railway, put
it in a variable, and never paste it into a file or a commit.

```powershell
# PowerShell
$env:PGURL = (railway variables --service Postgres --environment production --json `
              | ConvertFrom-Json).DATABASE_PUBLIC_URL
```

```bash
# bash
export PGURL=$(railway variables --service Postgres --environment production --json \
               | python -c "import json,sys; print(json.load(sys.stdin)['DATABASE_PUBLIC_URL'])")
```

Sanity-check that it now has a host and a port (this prints no secret):

```bash
python -c "import os;from urllib.parse import urlparse as u;p=u(os.environ['PGURL']);print(p.hostname, p.port)"
```

If that prints `None None`, the proxy is still off — go back to step 1.

### 3. Dump

```powershell
$stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
pg_dump --format=custom --no-owner --no-acl --file="kursi_$stamp.dump" $env:PGURL
```

`--format=custom` is compressed and lets you restore selectively with
`pg_restore`. For a plain-SQL dump you can read in a text editor, use
`--format=plain --file=kursi_$stamp.sql`.

Schema only / data only, when that is all you need:

```powershell
pg_dump --schema-only --file="schema_$stamp.sql" $env:PGURL
pg_dump --data-only   --format=custom --file="data_$stamp.dump" $env:PGURL
```

### 4. Verify the dump is real

A dump that silently produced nothing is worse than no dump. Check it:

```powershell
Get-Item "kursi_$stamp.dump" | Select-Object Length      # must not be ~0
pg_restore --list "kursi_$stamp.dump" | Select-String "TABLE DATA"
```

You should see `TABLE DATA` lines for `users`, `events`, `bookings`, `refunds`,
and `wishlist`.

### 5. Turn the proxy back off (optional but sensible)

If you enabled the proxy only to take a backup, disable it afterwards. It exposes
Postgres to the public internet for as long as it is on.

---

## Route B — dump inside the container via `railway ssh`

Useful if you would rather not expose a public endpoint at all. **Transport is
unverified from this network — see above.**

1. Confirm it connects at all:

```bash
railway ssh --service Postgres --environment production "echo OK"
```

If that hangs or fails with a connection/timeout error, this route is not
available from your network; use Route A.

2. Check the server version (so you install a matching `pg_dump` for restores):

```bash
railway ssh --service Postgres --environment production "pg_dump --version"
```

3. Stream a dump straight to a local file. `pg_dump` runs *inside* the container,
   where `DATABASE_URL` resolves, and stdout is piped back to you:

```bash
railway ssh --service Postgres --environment production \
  "pg_dump --format=custom --no-owner --no-acl \"\$DATABASE_URL\"" > kursi_$(date +%F_%H%M%S).dump
```

Then verify it with the same `pg_restore --list` check from Route A step 4.

> Redirect to a file — do not let a binary dump print to the terminal. And check
> the file size: if the SSH transport writes any banner text to stdout, the dump
> will be corrupt, which is another reason to run `pg_restore --list` on it.

---

## Restoring

**Never restore into the production database as a first move.** Restore into a
scratch database, confirm the data, and only then decide.

```powershell
# into a local scratch DB
createdb kursi_restore_test
pg_restore --dbname=postgresql://postgres:postgres@localhost:5432/kursi_restore_test `
           --no-owner --no-acl "kursi_2026-08-26_120000.dump"
```

For a real production restore, prefer Railway's dashboard backup/restore flow
(below) — it rebuilds the volume rather than replaying SQL into a live database.

---

## Automated backups (dashboard toggle)

**This is a user action in the Railway dashboard, not something code can do.**

Railway dashboard → project **proud-energy** → **Postgres** service →
**Backups** → enable scheduled backups, set the frequency (daily) and the
retention period.

Recommended: **daily, 7-day retention**, plus a manual dump (Route A) taken
immediately before any production migration — see `MIGRATIONS.md`.

Railway's scheduled backups are volume snapshots managed by the platform. They
are the primary safety net; the manual `pg_dump` above is a portable, off-platform
copy, which is the thing a snapshot cannot give you.

---

## Rules

- **Never** commit a dump file. `*.dump` and `*.sql` backups belong outside the
  repo (`.gitignore` already excludes `*.db`; keep dumps out of the working tree
  entirely).
- **Never** hardcode the connection string. Always read it from
  `railway variables` into a shell variable, as shown above.
- Dumps contain **real user data** (emails, booking records). Store them
  encrypted, and delete them when you are done.
