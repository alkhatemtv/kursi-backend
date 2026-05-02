"""Seed the database with sample events, organizers, and customers.

Run after first install:
    python seed.py

This is for local development only — do NOT run against production.
"""
import secrets
from datetime import datetime

from app.database import Base, SessionLocal, engine
from app.models import Booking, Event, User

Base.metadata.create_all(bind=engine)
db = SessionLocal()

# Wipe existing dev data so re-running the seed is idempotent
db.query(Booking).delete()
db.query(Event).delete()
db.query(User).delete()
db.commit()

# ── Users ──────────────────────────────────────────────────
organizer = User(
    auth0_sub="seed|organizer-1",
    email="organizer@kursi.io",
    name="Demo Organizer",
    role="organizer",
    org_name="Kursi Events Co.",
    phone="+965 2222 1111",
)
customer = User(
    auth0_sub="seed|customer-1",
    email="customer@kursi.io",
    name="Demo Customer",
    role="customer",
    phone="+965 9876 5432",
)
db.add_all([organizer, customer])
db.commit()
db.refresh(organizer)
db.refresh(customer)

# ── Events ─────────────────────────────────────────────────
events_data = [
    {
        "name": "Grand Theater Opening Night",
        "venue": "Kuwait National Theater, Salhiya",
        "event_date": "2026-06-14T20:00:00",
        "icon": "🎭",
        "tag": "Theater",
        "categories": [
            {"id": "vip", "name": "VIP", "price": 120, "color": "#f472b6"},
            {"id": "gold", "name": "Gold", "price": 60, "color": "#f59e0b"},
            {"id": "silver", "name": "Silver", "price": 35, "color": "#94a3b8"},
        ],
    },
    {
        "name": "Cinema Galaxy — Weekend Show",
        "venue": "Galaxy Cinemas, Avenues Mall",
        "event_date": "2026-06-20T19:30:00",
        "icon": "🎬",
        "tag": "Cinema",
        "categories": [
            {"id": "vip", "name": "VIP Recliners", "price": 25, "color": "#f472b6"},
            {"id": "premium", "name": "Premium", "price": 12, "color": "#f59e0b"},
            {"id": "standard", "name": "Standard", "price": 7, "color": "#94a3b8"},
        ],
    },
    {
        "name": "Kuwait Talk Show — Live Recording",
        "venue": "Bayan Palace Cultural Center",
        "event_date": "2026-07-05T21:00:00",
        "icon": "🎤",
        "tag": "Talk Show",
        "categories": [
            {"id": "front", "name": "Front Row VIP", "price": 80, "color": "#f472b6"},
            {"id": "premium", "name": "Premium", "price": 45, "color": "#f59e0b"},
            {"id": "general", "name": "General", "price": 20, "color": "#94a3b8"},
        ],
    },
]

for ev_data in events_data:
    # Generate a simple grid of seats spanning all categories
    seats = []
    seat_id = 1
    y = 0
    for cat in ev_data["categories"]:
        rows = 4
        cols = 12
        for r in range(rows):
            for c in range(cols):
                label = f"{cat['name'][0]}{chr(65 + r)}{c + 1}"
                seats.append({
                    "id": str(seat_id),
                    "x": c * 32,
                    "y": y + r * 32,
                    "catId": cat["id"],
                    "row": r,
                    "col": c,
                    "label": label,
                    "blocked": False,
                })
                seat_id += 1
        y += rows * 32 + 24  # gap between categories

    event = Event(
        organizer_id=organizer.id,
        name=ev_data["name"],
        venue=ev_data["venue"],
        event_date=ev_data["event_date"],
        icon=ev_data["icon"],
        tag=ev_data["tag"],
        seats=seats,
        categories=ev_data["categories"],
        status="active",
    )
    db.add(event)

db.commit()

# ── Sample bookings ───────────────────────────────────────
all_events = db.query(Event).all()
for ev in all_events:
    # Book the first 4 seats from the first category as a sample booking
    cat = ev.categories[0]
    booked_seats = []
    for seat in (ev.seats or [])[:4]:
        if seat["catId"] == cat["id"]:
            booked_seats.append({
                "label": seat["label"],
                "category": cat["name"],
                "price": cat["price"],
            })

    if booked_seats:
        booking = Booking(
            ref="KURSI-" + secrets.token_hex(3).upper(),
            customer_id=customer.id,
            event_id=ev.id,
            seats=booked_seats,
            total=sum(s["price"] for s in booked_seats),
            customer_name=customer.name,
            customer_email=customer.email,
            status="confirmed",
            payment_ref=f"FAKE-{secrets.token_hex(4)}",
        )
        db.add(booking)

db.commit()
db.close()

print("✓ Database seeded")
print(f"  - 2 users (organizer@kursi.io, customer@kursi.io)")
print(f"  - {len(events_data)} active events with full seat layouts")
print(f"  - {len(all_events)} sample bookings")
print()
print("NOTE: These users have synthetic auth0_subs ('seed|...').")
print("      Real users will be auto-created on first login via Auth0.")
