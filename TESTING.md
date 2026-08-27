# Testing

```bash
pip install -r requirements-dev.txt
pytest
```

That is the whole workflow. No database to set up, no services to start, no
network access required.

---

## The production database cannot be reached from a test run

This is enforced in `tests/conftest.py`, which pytest imports **before** any test
module and before anything under `app.` is imported — the only point at which
the engine's URL can still be decided.

Three layers:

1. **Inherited config is discarded.** Whatever `DATABASE_URL` is in your shell or
   in a local `.env` is popped from the environment. Tests never inherit it.
2. **The replacement must be a scratch database.** Either a throwaway SQLite file
   (`test_kursi_suite.db`, created and deleted per session) or an explicit
   `TEST_DATABASE_URL`. Nothing else is accepted.
3. **Live-looking hosts are refused outright.** The resolved URL is matched
   against known deployed-database markers (`rlwy.net`, `railway.internal`,
   `railway.app`, `amazonaws.com`, `supabase.co`, `neon.tech`). A match raises at
   import time, so **collection stops before a single table is touched**.

A final assertion confirms the app actually picked up the test URL before any
fixture runs.

Both layers are verified behaviours, not just intentions:

```bash
# A production URL in the shell is ignored — the suite runs on SQLite:
DATABASE_URL=postgresql://...@monorail.proxy.rlwy.net:12345/railway pytest    # passes

# Aiming TEST_DATABASE_URL at a live host is a hard stop:
TEST_DATABASE_URL=postgresql://...@monorail.proxy.rlwy.net:12345/railway pytest
# RuntimeError: REFUSING TO RUN TESTS: the resolved test database URL points at
# what looks like a live database (matched 'rlwy.net').
```

**Auth0 is never contacted.** JWT verification is patched at the dependency
boundary (`app.auth._decode_token`), so `_get_jwks()` — the only outbound HTTP
call in the auth path — is never reached. **Anthropic is never contacted**
either; `tests/test_ai.py` mocks the SDK client.

### Why a SQLite *file* and not `:memory:`

A pure `sqlite:///:memory:` database is per-connection: each connection the
TestClient opens would get its own empty database. A temp file gives every
connection the same schema, and the session fixture deletes it at the end. The
models use only portable column types (`String`, `Text`, `Integer`, `Float`,
`DateTime`, `JSON`), so SQLite is a faithful enough stand-in for the day-to-day
suite.

### Running against real Postgres

For dialect fidelity, point the suite at a **scratch** Postgres database:

```bash
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/kursi_test
pytest
```

The schema is dropped and recreated in that database, so it must be one you are
happy to lose. The guards above still apply.

---

## Fixtures (`tests/conftest.py`)

| Fixture | Scope | What it gives you |
|---|---|---|
| `_schema` | session, autouse | Creates the schema once; drops it and removes the temp DB at the end |
| `db` | function | A `Session` with every table emptied first (FK-safe order) |
| `client` | function | `TestClient` on a freshly emptied database |
| `fake_jwt` | function | Patches `_decode_token`; real user lookup/provisioning still runs |
| `seed` | function | One org-less user + one `active` event with 6 seats and 2 categories |

Helpers `make_token()` and `auth_header(sub=..., role=..., email=...)` build the
fixture bearer tokens. They are opaque markers, not real JWTs.

```python
def test_something(client, seed, fake_jwt):
    r = client.get("/users/me", headers=auth_header(sub="auth0|alice", role="organizer"))
    assert r.status_code == 200
```

---

## What is covered

`tests/test_foundations.py` defines the harness — deliberately narrow, **not**
full coverage:

- **auth** — no token → 401; invalid token → 401; valid token → 200 (control case
  proving the 401s come from verification, not an unreachable route)
- **`GET /events`** — only `PUBLIC_STATUSES` (`active`, `coming_soon`,
  `scheduled`) appear; `draft` and `inactive` events are asserted absent
- **`GET /events/{id}`** — seats carry `id/x/y/catId/row/col/label/blocked`,
  categories carry `id/name/price/color`, plus `venue_info` / `price_range` /
  `related_events` / `capacity`
- **auto-provisioning** — a first authenticated request creates the `User` row
  from the token claims, and a second request reuses it rather than duplicating
- **`/health`** — reports `env` and the migration fields

Pre-existing suites `tests/test_api.py` (integration flows) and
`tests/test_ai.py` (AI endpoints, SDK mocked) still run unchanged.

---

## Engine schema tests (Phase 1a)

`tests/test_engine_schema.py` covers the DB-enforced invariants of the Kursi Engine
schema — see [`docs/SCHEMA.md`](docs/SCHEMA.md).

**These build their own database.** The invariants (two partial unique indexes and the
layout freeze trigger) exist only in the Alembic migration; `create_all()` does not
create triggers, and the legacy suites drop/recreate the shared schema between tests,
which would remove them. So this module runs `alembic upgrade head` against a dedicated
database and asserts against that — the invariants are tested exactly as production
will have them.

What runs where:

| Test group | SQLite (default) | PostgreSQL |
|---|---|---|
| Migration up/down/up round-trip | runs | runs |
| Exit test 4 — double-sell backstop | runs | runs |
| Exit test 5 — frozen layout immutability | runs | runs |
| Seat-lock uniqueness | runs | runs |
| Exit test 6 — money shape | runs | runs |
| `TestPostgresSpecificDDL` | **skipped, with reason** | runs |

SQLite supports partial unique indexes *and* triggers, so the three invariants are
genuinely enforced on both backends and nothing is silently skipped. Only the four
tests that inspect PostgreSQL-specific artefacts — the plpgsql function, `pg_indexes`
predicates, and `TEXT[]` — are gated, and they hard-skip with a visible reason rather
than passing vacuously.

### Running the PostgreSQL-gated tests

They run automatically when `TEST_DATABASE_URL` points at a PostgreSQL database:

```bash
export TEST_DATABASE_URL=postgresql://user:pass@host:5432/kursi_scratch
pytest tests/test_engine_schema.py
```

> The module runs `alembic downgrade base` then `upgrade head` on that URL, so it
> **must** be a scratch database you are happy to have rebuilt. The guards in
> `conftest.py` still refuse any live-looking host.

**These are intended to run against the staging environment's database** once that
environment exists (see the Railway dashboard actions in the Phase 0 report). Until
then they skip locally by design — no local PostgreSQL or Docker is required or
expected.

---

## Engine service tests (Phase 1b)

`tests/engine/` covers the domain services in `app/engine_services/` — provisioning,
publishing, the locking engine and fulfilment. Like the Phase 1a schema tests, **the
package builds its own database with `alembic upgrade head`**: the services lean on
two partial unique indexes and the layout-freeze trigger, and `create_all()` does not
create triggers.

Two further harness details, both in `tests/engine/conftest.py`:

- **A hand-driven clock is installed for every test** (`ManualClock`, autouse). Expiry
  is timestamp truth, so tests move time instead of sleeping — and installing it
  everywhere, not just in the expiry tests, means a service that read the wall clock
  directly would fail here rather than only in production.
- **SQLite runs with `BEGIN IMMEDIATE`, `busy_timeout` and `foreign_keys=ON`.** Without
  the first two, two threads writing concurrently hit "database is locked" instead of
  queueing; with them, concurrent writers serialise cleanly. This does *not* give SQLite
  PostgreSQL's concurrency model, and the tests say so where it matters.

What runs where:

| Test group | SQLite (default) | PostgreSQL |
|---|---|---|
| Provisioning: idempotency, slugs, single-transaction | runs | runs |
| Provisioning: 2 and 5 concurrent first requests (real threads) | runs | runs |
| Publishing: freeze, 144-seat materialisation, republish idempotency | runs | runs |
| Locking: all-or-nothing, conflict shapes, `external_ref` idempotency | runs | runs |
| Exit test 1 — two threads, one seat, one winner | runs | runs |
| Exit test 1 — loser **blocks on the index** mid-transaction | **skipped, with reason** | runs |
| Exit test 2 — expiry without GC | runs | runs |
| Exit test 3 — the single +4:00 | runs | runs |
| Fulfilment: tickets, usage, cancel/refund, credentials | runs | runs |
| Clock + unit of work | runs | runs |

Exactly one test is gated. Two threads contending for one seat runs on both backends
and proves the arbiter (the partial unique index picks the winner; the loser gets the
structured conflict) — but on SQLite the two write transactions are *serialised*, so
the loser meets an already-committed lock rather than blocking on the index. The
interleaved case — the competing `INSERT` waiting inside the index until the winner
commits — is genuinely inexpressible on SQLite and is therefore skipped there with a
visible reason, not approximated with sequential calls.

```bash
export TEST_DATABASE_URL=postgresql://user:pass@host:5432/kursi_scratch
pytest tests/engine
```

> The package runs `alembic downgrade base` then `upgrade head` on that URL, so it must
> be a scratch database. The `conftest.py` guards still refuse any live-looking host.

### The legacy suites now clear Engine tables too

`get_current_user` provisions a personal organization, so every authenticated request in
`test_api.py` / `test_foundations.py` leaves `engine_organizations` and
`engine_memberships` rows behind. The `db` fixture wipes them alongside the legacy tables
(`_ENGINE_WIPE_ORDER`) — `engine_memberships.user_id` is an FK onto `users.id` with
`ON DELETE RESTRICT`, so leaving them would break the user wipe on PostgreSQL.

---

## Notes

- `httpx` is already a production dependency (the auth layer uses it for JWKS),
  so the FastAPI `TestClient` adds no extra test-only package. `pytest` is the
  only addition, in `requirements-dev.txt`.
- The `StarletteDeprecationWarning` about `httpx2` and the
  `datetime.utcnow()` `DeprecationWarning`s are pre-existing and unrelated to
  these tests.
