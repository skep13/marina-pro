"""Example bookings for demos: reset the store and add a handful of stays
around today, including QX7M4T for Sarah Collins, arriving tomorrow for
three nights, so a demo can look up and change a known booking."""
from datetime import timedelta

from process.hotel import profile
from process.hotel.store import _now_iso, booking_system

SEED = [
    # ref, first, last, room, arrives in (days), nights, guests, channel
    ("QX7M4T", "Sarah", "Collins", "harbour_view", 1, 3, 2, "website"),
    ("MP3KDW", "Tom", "Reid", "classic_double", 0, 2, 2, "phone"),
    ("HC9VNA", "Priya", "Shah", "garden_family", 0, 4, 4, "website"),
    ("RW4TJE", "Daniel", "Okafor", "classic_double", 2, 1, 1, "reception"),
    ("FN7GXU", "Emma", "Walsh", "harbour_view", 5, 2, 2, "website"),
]


def reset():
    sys = booking_system()
    today = profile.now().date()
    with sys._lock, sys._db() as db:
        for table in ("activity_bookings", "handoffs", "bookings"):
            db.execute(f"DELETE FROM {table}")
        for ref, first, last, room_id, arrives, nights, guests, channel in SEED:
            room = profile.room_types()[room_id]
            ci = today + timedelta(days=arrives)
            co = ci + timedelta(days=nights)
            total = sys._quote(room, ci, co)
            now = _now_iso()
            db.execute(
                "INSERT INTO bookings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ref, "confirmed", room_id, ci.isoformat(), co.isoformat(), guests,
                 first, last, f"{first.lower()}@guest.example", "", total,
                 profile.currency(), channel, now, now))
    return len(SEED)

