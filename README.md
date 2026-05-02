# Kursi.io Backend — FastAPI + SQLite + Auth0

A production-shaped backend for the Kursi.io event ticketing platform.

- **Framework**: FastAPI
- **Database**: SQLite (dev) → PostgreSQL drop-in for prod
- **Auth**: Auth0 (RS256 JWTs verified against JWKS)
- **Payments**: faked (no Stripe) — `payment_ref` is a mock string

---

## Quick start

```bash
# 1. Install
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
#   Edit .env and fill in your Auth0 values (see "Auth0 setup" below).
#   Until you do that, every authenticated route will return 401.
#   Public routes (GET /events, /health) work without Auth0.

# 3. Seed sample data (optional but recommended for first run)
python seed.py

# 4. Run the server
uvicorn app.main:app --reload --port 8000
```

Open these once it's running:

- API root:        <http://localhost:8000/>
- Interactive docs: <http://localhost:8000/docs> ← try requests here
- ReDoc:           <http://localhost:8000/redoc>

---

## Project layout

```
kursi-backend/
├── app/
│   ├── main.py              ← FastAPI entry point + CORS + table creation
│   ├── config.py            ← env-var settings via pydantic-settings
│   ├── database.py          ← SQLAlchemy engine + session
│   ├── models.py            ← User, Event, Booking, Refund tables
│   ├── schemas.py           ← Pydantic request/response shapes
│   ├── auth.py              ← Auth0 JWT verification + auto user creation
│   └── routers/
│       ├── users.py         ← /users/me
│       ├── events.py        ← /events (CRUD)
│       ├── bookings.py      ← /bookings (purchase + list)
│       └── refunds.py       ← /refunds (organizer-initiated)
├── tests/
│   └── test_api.py          ← 10 integration tests (auth mocked)
├── seed.py                  ← populate DB with sample events
├── requirements.txt
├── .env.example             ← copy to .env
└── README.md
```

---

## Routes reference

### Public (no auth)

| Method | Path                | Purpose                          |
|--------|---------------------|----------------------------------|
| GET    | `/`                 | Health check                     |
| GET    | `/health`           | Health check                     |
| GET    | `/events`           | List **active** events (homepage)|
| GET    | `/events/{id}`      | View one event                   |

### Authenticated (any role)

| Method | Path                       | Purpose                              |
|--------|----------------------------|--------------------------------------|
| GET    | `/users/me`                | Current user (auto-created on first call) |
| PUT    | `/users/me`                | Update profile / role                |
| POST   | `/bookings`                | Purchase tickets (fake payment)      |
| GET    | `/bookings/mine`           | My booking history                   |
| GET    | `/bookings/{id}`           | View one booking (customer or organizer) |

### Organizer-only

| Method | Path                              | Purpose                       |
|--------|-----------------------------------|-------------------------------|
| GET    | `/events/mine/list`               | All my events (any status)    |
| POST   | `/events`                         | Create event                  |
| PUT    | `/events/{id}`                    | Update one of my events       |
| DELETE | `/events/{id}`                    | Delete one of my events       |
| GET    | `/bookings/event/{event_id}`      | Bookings on one of my events  |
| POST   | `/refunds`                        | Initiate refund               |
| POST   | `/refunds/{id}/approve`           | Approve refund (frees seats)  |
| POST   | `/refunds/{id}/reject`            | Reject refund                 |
| GET    | `/refunds/mine`                   | All refunds across my events  |

---

## Auth0 setup (one-time)

### 1. Create the API in Auth0

1. Sign in at <https://manage.auth0.com>.
2. **Applications → APIs → Create API**.
3. Name: `Kursi API`. Identifier: `https://api.kursi.io` (must match `AUTH0_API_AUDIENCE`). Signing algorithm: `RS256`.

### 2. Create a Single-Page Application

1. **Applications → Applications → Create Application**.
2. Name: `Kursi Web`. Type: **Single Page Web Applications**.
3. In its settings:
   - **Allowed Callback URLs**: your frontend origin (e.g. `http://localhost:5500, https://kursi.io`)
   - **Allowed Logout URLs**: same
   - **Allowed Web Origins**: same
4. Save and copy the **Domain** and **Client ID** — your frontend will use these.

### 3. Add a custom-claims Action (for roles)

Auth0 doesn't include the user's app role in tokens by default. Add it via an Action:

1. **Actions → Library → Build Custom**.
2. Name: `Add role to access token`. Trigger: **Login / Post Login**.
3. Paste:

```javascript
exports.onExecutePostLogin = async (event, api) => {
  const namespace = "https://kursi.io/";
  const role = event.user.app_metadata?.role || "customer";
  api.accessToken.setCustomClaim(`${namespace}role`, role);
  api.accessToken.setCustomClaim(`${namespace}email`, event.user.email);
  if (event.user.name) {
    api.accessToken.setCustomClaim(`${namespace}name`, event.user.name);
  }
};
```

4. **Deploy → Add to flow** (Login flow → drag the action between Start and Complete).

### 4. Mark a user as an organizer

After a user signs up:

1. **User Management → Users → click the user**.
2. Scroll to **app_metadata**, paste:
   ```json
   { "role": "organizer" }
   ```
3. Save. Their next login will get `https://kursi.io/role: "organizer"` in the token.

(Self-service: a user can also `PUT /users/me` with `{"role": "organizer"}` to elevate themselves. In production you'd guard this behind verification.)

### 5. Fill in `.env`

```env
AUTH0_DOMAIN=your-tenant.auth0.com
AUTH0_API_AUDIENCE=https://api.kursi.io
AUTH0_NAMESPACE=https://kursi.io/
```

---

## Connecting your `index.html` frontend

In your existing `index.html`, install the Auth0 SPA SDK and replace the demo login with a real one. Minimal sketch:

```html
<script src="https://cdn.auth0.com/js/auth0-spa-js/2.1/auth0-spa-js.production.js"></script>
<script>
  const API_BASE = "http://localhost:8000";  // your backend
  let auth0Client;

  async function initAuth() {
    auth0Client = await auth0.createAuth0Client({
      domain: "your-tenant.auth0.com",
      clientId: "YOUR_CLIENT_ID",
      authorizationParams: {
        redirect_uri: window.location.origin,
        audience: "https://api.kursi.io",
      },
      cacheLocation: "localstorage",
    });

    // Handle the callback after login
    if (window.location.search.includes("code=")) {
      await auth0Client.handleRedirectCallback();
      window.history.replaceState({}, document.title, "/");
    }
  }

  // Use this everywhere your old code called localStorage.getItem('kursi-events') etc.
  async function api(path, opts = {}) {
    const token = await auth0Client.getTokenSilently();
    const res = await fetch(API_BASE + path, {
      ...opts,
      headers: {
        ...(opts.headers || {}),
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
    });
    if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
    return res.json();
  }

  // Examples:
  //   const events = await api('/events/mine/list');                  // organizer's events
  //   await api('/events', { method: 'POST', body: JSON.stringify({...}) });  // create
  //   await api('/events/' + id, { method: 'DELETE' });               // delete
  //   await api('/bookings', { method: 'POST', body: JSON.stringify({event_id, seats}) });

  initAuth();
</script>
```

For login/logout buttons:

```js
document.getElementById("login").onclick = () => auth0Client.loginWithRedirect();
document.getElementById("logout").onclick = () =>
  auth0Client.logout({ logoutParams: { returnTo: window.location.origin } });
```

---

## Running tests

```bash
pip install pytest
pytest tests/ -v
```

The tests mock Auth0 so they don't need a real tenant. They cover:

- Health check
- Auth: token rejection, auto-user-creation on first call
- Events: create / list / update / delete + ownership checks + role gating
- Bookings: full purchase flow with seat-conflict detection
- Refunds: approve flow that frees up the seat for re-booking

---

## Going to production

When you're ready:

1. **Switch to PostgreSQL.** Change one line in `.env`:
   ```
   DATABASE_URL=postgresql://user:pass@host:5432/kursi
   ```
   No code changes needed — SQLAlchemy handles both.

2. **Add Alembic for migrations.** Right now `Base.metadata.create_all()` works for dev but won't handle schema changes. `pip install alembic` and `alembic init` when you need versioned migrations.

3. **Tighten CORS.** Set `CORS_ORIGINS` to only your real frontend domain.

4. **Deploy.** [Railway](https://railway.app), [Render](https://render.com), or [Fly.io](https://fly.io) all deploy from a git push. The included `requirements.txt` is all they need; their default Python build will run `uvicorn app.main:app`.

5. **Add real payments later.** When you're ready, `payment_ref` is already wired through — swap the fake string for a Stripe `payment_intent.id` and you're done.

---

## Common gotchas

- **403 on `/users/me`**: you didn't send the `Authorization: Bearer ...` header.
- **401 "Invalid token"**: `AUTH0_DOMAIN` or `AUTH0_API_AUDIENCE` doesn't match what your frontend is requesting from Auth0.
- **403 "Organizer role required"**: the JWT has no `role` claim. Either complete step 3 (the Action) or `PUT /users/me {"role": "organizer"}` to flip it manually.
- **CORS error in browser**: add your frontend origin to `CORS_ORIGINS` and restart.
