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

## Notes

- `httpx` is already a production dependency (the auth layer uses it for JWKS),
  so the FastAPI `TestClient` adds no extra test-only package. `pytest` is the
  only addition, in `requirements-dev.txt`.
- The `StarletteDeprecationWarning` about `httpx2` and the
  `datetime.utcnow()` `DeprecationWarning`s are pre-existing and unrelated to
  these tests.
