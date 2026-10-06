"""Example bookings for demos: reset the store and add a handful of stays
around today, including QX7M4T for Sarah Collins, arriving tomorrow for
three nights, so a demo can look up and change a known booking."""
from datetime import datetime, timedelta, timezone

from process.hotel import profile
from process.hotel.store import booking_system

SEED = [
    # ref, first, last, room, arrives in (days), nights, guests, channel,
    # booked how long ago (hours)
    ("QX7M4T", "Sarah", "Collins", "harbour_view", 1, 3, 2, "website", 30),
    ("MP3KDW", "Tom", "Reid", "classic_double", 0, 2, 2, "phone", 5.5),
    ("HC9VNA", "Priya", "Shah", "garden_family", 0, 4, 4, "website", 2.2),
    ("RW4TJE", "Daniel", "Okafor", "classic_double", 2, 1, 1, "reception", 1.3),
    ("FN7GXU", "Emma", "Walsh", "harbour_view", 5, 2, 2, "website", 0.6),
]


def reset():
    sys = booking_system()
    today = profile.now().date()
    with sys._lock, sys._db() as db:
        for table in ("activity_bookings", "handoffs", "outbox", "bookings"):
            db.execute(f"DELETE FROM {table}")
        for ref, first, last, room_id, arrives, nights, guests, channel, ago in SEED:
            room = profile.room_types()[room_id]
            ci = today + timedelta(days=arrives)
            co = ci + timedelta(days=nights)
            total = sys._quote(room, ci, co)
            made = (datetime.now(timezone.utc) - timedelta(hours=ago)).isoformat(timespec="seconds")
            db.execute(
                "INSERT INTO bookings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ref, "confirmed", room_id, ci.isoformat(), co.isoformat(), guests,
                 first, last, f"{first.lower()}@guest.example", "", total,
                 profile.currency(), channel, made, made))
    return len(SEED)

