# Kursi Engine — Phase 1 Migration Spec (schema design, v1)

Implements the five locked decisions:
(1) everyone-is-an-org, memberships many-to-many · (2) performance owns seating via
immutable layout versions · (3) session-grouped locks, 8+4 min, DB-timestamp truth ·
(4) stable ticket ID + rotatable credentials, monotonic usage · (5) integer minor units
+ ISO currency.

Conventions: all PKs `id` BIGINT identity (except where noted); all tenant tables carry
`organization_id` FK + index; `created_at/updated_at` timestamptz on everything (omitted
below for brevity); soft business states as TEXT + CHECK constraints (readable in SQL,
extensible); JSONB for document-shaped data. All money = `amount_minor BIGINT` +
`currency CHAR(3)`. Layout coordinate contract unchanged: 50 px/metre, x/y in canvas
units, identical to current v2/backend values.

---

## 1. Tenancy

**organizations** — id, name, name_ar, slug UNIQUE, type CHECK(personal|business),
plan TEXT (personal|business|pro|enterprise), branding JSONB, settings JSONB,
status CHECK(active|suspended).

**users** — EXISTING table kept (auth0_sub UNIQUE, email, name, phone). REMOVE nothing
in Phase 1; `role` column becomes legacy (memberships supersede it) — kept, ignored by
new code, dropped in a later cleanup migration.

**memberships** — organization_id, user_id, role CHECK(owner|admin|event_manager|
venue_manager|box_office|finance|support|scanner|marketing), status CHECK(active|
invited|disabled), UNIQUE(organization_id, user_id).

Provisioning rule (code, Phase 1): on first authenticated request, auto-create user
(existing behavior) AND a personal organization + owner membership if the user has none.

## 2. Venues & layouts

**venues** — organization_id, name, name_ar, address, address_ar, timezone TEXT
(default 'Asia/Kuwait').

**venue_layouts** — venue_id, name (e.g. "Main Hall — Full"), description.

**layout_versions** — venue_layout_id, version_number INT, status CHECK(draft|frozen),
frozen_at, created_by_user_id, layout_data JSONB (seats[], objects[], categories[]
definitions with colors; the authoring document), UNIQUE(venue_layout_id, version_number).
RULE (enforced in DB, not convention): a trigger/constraint rejects UPDATE of
layout_data when status='frozen'. Freezing happens automatically the first time a
performance generates inventory from the version (transactionally, in code) — never
manually unfrozen; edits create the next version_number as draft.

## 3. Events & performances

**events** — organization_id, venue_id NULLABLE FK, title, title_ar, description,
description_ar, artwork_url, cover_url, category TEXT, cast JSONB (per §10),
policies JSONB (terms, refund policy, age restriction, instructions),
status CHECK(draft|active|coming_soon|scheduled|cancelled|archived).

**performances** — event_id, layout_version_id FK (the seating source of truth),
starts_at timestamptz, doors_open_at, duration_minutes,
sales_open_at, sales_close_at, box_office_close_at,
status CHECK(draft|on_sale|paused|sold_out|closed|cancelled).
Inventory generation: creating/publishing a performance materializes
performance_seats from its layout_version (freezing it if still draft).

**performance_categories** — performance_id, category_key TEXT (matches key in
layout_data), name, name_ar, color, amount_minor, currency,
UNIQUE(performance_id, category_key). (Per-performance pricing per §13; the layout holds
geometry + default category identity, the performance holds price.)

**performance_seats** (the inventory) — performance_id, seat_uid TEXT (stable id from
layout_data), section, row_label, seat_number, label (display, e.g. "A-12"), x, y,
category_key, status CHECK(available|blocked|invitation|reserved_internal),
price_override_minor NULLABLE, currency, accessibility BOOL,
UNIQUE(performance_id, seat_uid), INDEX(performance_id, status).
NOTE: `sold` and `locked` are NOT statuses here — sold is the existence of an issued
ticket; locked is the existence of an active lock (see §4/§5). This prevents status
drift between tables. Availability = status='available' AND no active lock AND no
live ticket.

## 4. Checkout & locking

**orders** — organization_id, performance_id, channel TEXT CHECK(marketplace|api|
box_office|comp|invitation), status CHECK(draft|awaiting_payment|completed|expired|
cancelled), customer_name, customer_email, customer_phone (nullable until capture),
expires_at timestamptz NULLABLE (authoritative for its locks), extended BOOL DEFAULT
false (the single +4:00), subtotal_minor, fees_minor, discount_minor, total_minor,
currency, external_ref TEXT (client/API idempotency key, UNIQUE per org NULLABLE).
State machine: draft → awaiting_payment → completed; draft/awaiting_payment → expired
(expires_at passed) | cancelled. Completed is terminal-success.

**seat_locks** — order_id, performance_seat_id, released_at NULLABLE.
PARTIAL UNIQUE INDEX on (performance_seat_id) WHERE released_at IS NULL — the database
race arbiter: two simultaneous lock attempts on A-12, one INSERT wins.
A lock is ACTIVE iff released_at IS NULL AND its order.expires_at > now() AND order
status IN (draft, awaiting_payment). No sweeper participates in correctness; a GC job
may set released_at on long-dead rows for hygiene.
Extension: single UPDATE of orders.expires_at (+4 min), guarded by extended=false and a
progress condition (payment step reached), covering all the order's locks atomically.

## 5. Tickets

**tickets** — order_id, organization_id, performance_id, performance_seat_id,
status CHECK(issued|checked_in|cancelled|refunded),
credential_version INT DEFAULT 1, credential_hash TEXT (hash of current signed token),
issued_at, checked_in_at NULLABLE, checked_in_by_user_id NULLABLE,
amount_paid_minor, currency,
PARTIAL UNIQUE INDEX on (performance_seat_id) WHERE status IN ('issued','checked_in')
— the never-double-sell backstop at the data layer.
QR = opaque signed token → {ticket_id, credential_version}; scanner resolves via API;
version mismatch ⇒ "superseded credential". Rotation: credential_version++, new hash,
audit event; ticket_id and status unchanged.
Scan verdicts (API layer): valid | already_checked_in | cancelled | refunded |
wrong_performance | invalid | superseded.

## 6. Usage, audit, keys, webhooks

**usage_events** — organization_id, ticket_id UNIQUE, occurred_at. One row per issued
ticket, never deleted (monotonic per Decision 4; cancellation/refund do NOT remove).
Monthly counters are SELECTs over this table; billing math in code.

**audit_log** — organization_id, actor_user_id NULLABLE, actor_api_key_id NULLABLE,
action TEXT, entity_type, entity_id, data JSONB, occurred_at. Append-only. Phase 1
writes: lock/extend/expire, order transitions, ticket issue/rotate/check-in/cancel/
refund, layout freeze, performance publish.

**api_keys** — organization_id, name, key_prefix TEXT (visible), key_hash TEXT,
environment CHECK(sandbox|production), scopes TEXT[], last_used_at, revoked_at NULLABLE.

**webhook_endpoints** — organization_id, url, secret, events TEXT[], active BOOL.
**webhook_deliveries** — endpoint_id, event_type, payload JSONB, status CHECK(pending|
delivered|failed), attempts INT, last_attempt_at. (Delivery worker is Phase 3; schema
lands now so audit/order code can enqueue.)

Deferred to Phase 4 (no schema now, nothing above blocks them): promo_codes,
invitations, credit packs.

## 7. Migration path for existing production data

Old tables (users, events, bookings, refunds, wishlist) are NOT dropped. The frozen
marketplace keeps reading old routes/tables untouched. New tables land alongside;
new API is namespaced /v1/. Data carried forward as Engine fixtures:

1. users → unchanged (same rows serve both worlds).
2. For user id 3 (organizer): create organization "Kursi Events" (type business for
   realism), owner membership.
3. Each old event (1,2,3) → venue (from venue string) + venue_layout + layout_version v1
   (layout_data built from old seats/categories JSON — same coordinates, 50 px/m) +
   new event + ONE performance at event_date, layout frozen on inventory generation,
   performance_seats materialized, performance_categories priced from old categories
   (KWD → amount_minor ×1000).
4. Old bookings → completed orders + issued tickets (+usage_events), seat matched by
   label; unmatchable rows logged, not guessed.
5. refunds/wishlist: left on legacy tables; wishlist is a marketplace concern (later),
   refunds map when refund flows build in Phase 3/4.
Executed as an Alembic data migration, run on STAGING first against a restored prod
dump, verified, then production — after a fresh manual backup.

---

## 8. Exit tests (Phase 1 is done when these pass on staging)

1. RACE: two concurrent lock attempts on one seat → exactly one succeeds; loser gets a
   structured conflict (pytest with real Postgres via TEST_DATABASE_URL, threads).
2. EXPIRY: locks dead the microsecond expires_at passes (timestamp truth, no sweeper);
   expired order's seats immediately lockable by another order.
3. EXTENSION: exactly one +4:00 per order; second attempt rejected; all seats share the
   one expiry.
4. DOUBLE-SELL BACKSTOP: forcing two issued tickets on one performance_seat violates
   the partial unique index.
5. IMMUTABILITY: UPDATE layout_data on a frozen version rejected by the DB; editing
   creates version n+1 draft; live performance still reads its frozen version.
6. MONEY: all monetary columns integer; API rejects float/decimal money input; KWD
   round-trips 5.500 ⇄ 5500.
7. CREDENTIAL: rotation invalidates old token (superseded), ticket_id/status stable,
   usage count unchanged.
8. MIGRATION: staging restore of prod dump → data migration → 3 orgs' worth of
   venues/events/performances/seats/tickets match legacy counts (144 seats each,
   bookings ⇒ tickets 1:1).
