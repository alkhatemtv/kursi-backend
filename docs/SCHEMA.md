# Kursi Engine schema map (Phase 1a)

The authoritative design is [`engine-phase1-schema-spec.md`](engine-phase1-schema-spec.md).
This page is the one-screen orientation for future sessions: what exists, where the
boundary with the legacy marketplace runs, and the three invariants the *database*
enforces.

- Models: `app/engine_models.py` (legacy models stay in `app/models.py`, untouched)
- Migration: `alembic/versions/63446a371e5a_engine_phase_1a_schema.py`
- Revision graph: `d29b3ede11f0` (baseline) → **`63446a371e5a`** (this)

---

## Legacy vs Engine: the `engine_` prefix

A legacy `events` table already exists and is read by the frozen marketplace, so the
Engine's own `events` table would collide with it. **Every Engine table is prefixed
`engine_`.** No Postgres schema/namespace is used - see
[Why a prefix](#why-a-prefix-and-not-a-postgres-schema).

```
LEGACY (frozen - do not modify)        ENGINE (engine_*)
  users  ←──────────────────────────┐    engine_organizations
  events                            │    engine_memberships ──┘ (user_id → users.id)
  bookings                          │    engine_venues
  refunds                           ├──── engine_layout_versions.created_by_user_id
  wishlist                          ├──── engine_tickets.checked_in_by_user_id
                                    └──── engine_audit_log.actor_user_id
```

`users` is the one table shared by both worlds (spec §1): the same rows serve the
marketplace and the Engine, so four Engine tables carry FKs into it. Note `users.id` is
INTEGER (legacy), so those FK columns are `Integer`, not `BigInteger`.

`users.role` is now legacy — `engine_memberships` supersedes it. It is kept and ignored
by new code, and dropped in a later cleanup migration.

## The 17 Engine tables

| § | Table | Purpose |
|---|---|---|
| 1 | `engine_organizations` | Tenant. Everyone is an org; personal orgs auto-provision. |
| 1 | `engine_memberships` | user ↔ org with a role. `UNIQUE(organization_id, user_id)`. |
| 2 | `engine_venues` | Physical place. `timezone` defaults to `Asia/Kuwait`. |
| 2 | `engine_venue_layouts` | A named seating arrangement of a venue. |
| 2 | `engine_layout_versions` | Versioned authoring document (`layout_data` JSONB). **Immutable once frozen.** |
| 3 | `engine_events` | The Engine's event. Distinct from legacy `events`. |
| 3 | `engine_performances` | One dated showing; points at the layout version that is its seating truth. |
| 3 | `engine_performance_categories` | Per-performance pricing per category key. |
| 3 | `engine_performance_seats` | The inventory: one seat, one performance. |
| 4 | `engine_orders` | Checkout session. `expires_at` is authoritative for its locks. |
| 4 | `engine_seat_locks` | Held seats. **The race arbiter.** |
| 5 | `engine_tickets` | Sold seats. Stable id, rotatable credentials. |
| 6 | `engine_usage_events` | One row per issued ticket, never deleted (billing is monotonic). |
| 6 | `engine_audit_log` | Append-only actions. Actor FKs are `SET NULL`. |
| 6 | `engine_api_keys` | Hashed keys, `scopes TEXT[]`, sandbox/production. |
| 6 | `engine_webhook_endpoints` | Subscriptions. Delivery worker is Phase 3. |
| 6 | `engine_webhook_deliveries` | Queued attempts. |

Ownership chain: `organization → venue → venue_layout → layout_version`, and
`organization → event → performance → performance_seats`, with `performance` bound to a
frozen `layout_version`.

---

## The three DB-enforced invariants

These are the point of Phase 1a. They live in the database, not in Python, so no
application bug and no raw SQL can violate them.

### 1. One active lock per seat

```sql
CREATE UNIQUE INDEX uq_engine_seat_locks_active_seat
  ON engine_seat_locks (performance_seat_id) WHERE released_at IS NULL;
```

Two concurrent lock attempts on seat A-12 become two INSERTs; the database picks the
winner and the loser gets a constraint violation. **No application-level check
participates in correctness.** Setting `released_at` drops the row out of the index, so
the seat is immediately lockable again — expiry needs no sweeper.

A lock is *active* iff `released_at IS NULL` **and** its order's `expires_at` is in the
future **and** that order is `draft`/`awaiting_payment`.

### 2. Never double-sell

```sql
CREATE UNIQUE INDEX uq_engine_tickets_live_seat
  ON engine_tickets (performance_seat_id) WHERE status IN ('issued','checked_in');
```

At most one *live* ticket per seat. `cancelled` and `refunded` tickets fall outside the
predicate, which frees the seat for resale while preserving the historical row.

### 3. Frozen layouts are immutable

A trigger rejects both:
- `UPDATE` of `layout_data` while `status = 'frozen'`, and
- any `status` change out of `'frozen'` (freezing is one-way).

`draft → frozen` stays legal, because at that moment `OLD.status` is still `'draft'`.
Editing a frozen layout means creating `version_number + 1` as a new draft; the live
performance keeps reading its own frozen version.

Implemented per dialect: a plpgsql `BEFORE UPDATE` trigger on PostgreSQL, and two
`RAISE(ABORT)` triggers on SQLite. Postgres surfaces the failure as
`sqlalchemy.exc.InternalError`, SQLite as `IntegrityError`; both are `DBAPIError`, which
is what the tests assert.

### Supporting uniqueness

| Rule | Mechanism |
|---|---|
| `usage_events.ticket_id` unique | column `UNIQUE` — re-issuing can never double-bill |
| `orders.external_ref` unique per org, nullable many times | partial unique index `WHERE external_ref IS NOT NULL` |
| `memberships` one row per (org, user) | `UNIQUE(organization_id, user_id)` |
| `performance_seats` one row per (performance, seat_uid) | `UNIQUE(performance_id, seat_uid)` |
| `layout_versions` one row per (layout, version_number) | `UNIQUE(venue_layout_id, version_number)` |

---

## Conventions

- **PKs** — `BIGINT GENERATED BY DEFAULT AS IDENTITY` on Postgres; SQLite falls back to
  its INTEGER rowid alias (`Identity()` is ignored there).
- **Timestamps** — `created_at` / `updated_at` `timestamptz` on every table.
- **States** — `TEXT` + `CHECK`, never a native enum: readable in plain SQL and
  extensible without an `ALTER TYPE`.
- **Documents** — `JSONB` (degraded to `JSON` on SQLite).
- **Money** — `amount_minor BIGINT` + `currency CHAR(3)`, always. See below.
- **FKs** — `ON DELETE RESTRICT` everywhere, with two deliberate exceptions:
  - `webhook_deliveries.endpoint_id` → `CASCADE`: a delivery attempt is meaningless
    once its endpoint is gone, and deliveries are transient operational rows.
  - actor columns (`tickets.checked_in_by_user_id`, `audit_log.actor_user_id`,
    `audit_log.actor_api_key_id`) → `SET NULL`: the record must outlive the actor.

### Seat status has no `sold` or `locked`

`engine_performance_seats.status` is only `available | blocked | invitation |
reserved_internal`. **Sold** is the existence of a live ticket; **locked** is the
existence of an active lock. Keeping those out of the column is what prevents status
drift between tables.

```
availability = status = 'available'  AND  no active lock  AND  no live ticket
```

### Money can never be silently rounded

Money uses `MinorAmount` (`app/engine_models.py`), a `TypeDecorator` over `BigInteger`
that **rejects** floats and Decimals at bind time — for ORM flushes and Core inserts
alike, on every dialect.

This matters because a bare `BIGINT` column does *not* give you that: PostgreSQL coerces
`100.5` to `101`, and SQLite stores it verbatim. Both are silent corruption of money.
Callers convert explicitly: KWD `5.500` → `5500`. `CHECK (… >= 0)` and
`CHECK (length(currency) = 3)` back it up in the database.

---

## Why a prefix and not a Postgres schema

Both were viable. The prefix won on three counts:

1. **The SQLite test path survives.** SQLite has no real multi-schema support, so a
   dedicated namespace would force every Engine test onto Postgres. With the prefix,
   the whole schema — including both partial unique indexes and the freeze trigger —
   is creatable and testable on SQLite, so the invariant tests run on every laptop.
2. **Nothing about the legacy setup changes.** The repo has zero schema qualification
   today; a namespace would mean `search_path` handling and `include_schemas` in
   Alembic autogenerate.
3. **The boundary is visible at the call site.** `engine_events` versus `events` reads
   unambiguously in queries, models, and migrations.

Cost: the table names are longer, and if the legacy tables are eventually retired the
prefix becomes vestigial. Renaming then is a mechanical migration.

---

## Out of scope for Phase 1a

No API routes, no locking/order business logic (1b), no endpoints (1c), no data
migration of legacy rows (1d — spec §7), no org auto-provisioning code change. The
schema supports all of them; none of them are implemented here.

Deferred entirely to Phase 4 (no schema yet): promo codes, invitations, credit packs.
