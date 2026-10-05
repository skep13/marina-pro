"""Bookings, activity bookings and staff handoffs.

`BookingSystem` is what the rest of Marina talks to. `LocalBookingSystem`
keeps everything in a SQLite file on the hotel's own machine, using the rooms
and activities in hotel.yaml. A property management system (Opera, Mews,
Cloudbeds and so on) plugs in by implementing the same methods; see
`booking_system()` at the bottom.

Errors meant for the guest are raised as BookingError, with a message Marina
can say out loud.
"""
import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

from process.config import load_config, resolve_data
from process.hotel import profile

MAX_NIGHTS = 30
MAX_DAYS_AHEAD = 400

# Booking references are read out over the phone, so no 0/O, 1/I/L, 5/S, 8/B.
_REF_ALPHABET = "ACDEFGHJKMNPQRTUVWXY234679"

HOLDS_ROOM = ("requested", "confirmed")


class BookingError(Exception):
    pass


def new_ref():
    return "".join(secrets.choice(_REF_ALPHABET) for _ in range(6))


_SPOKEN = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}


def normalise_ref(text):
    """'k 7 m-4 q x', 'K7M4QX.' and 'k seven m four q x' all become K7M4QX."""
    words = str(text or "").lower().replace("-", " ").replace(".", " ").split()
    return "".join(_SPOKEN.get(w, w) for w in words).upper()


def _day(value, what):
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise BookingError(f"I need the {what} as a date, like 2026-10-23.") from None


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _nights(check_in, check_out):
    d = check_in
    while d < check_out:
        yield d
        d += timedelta(days=1)


def nightly_rate(room, night):
    # Friday and Saturday nights are the weekend.
    if night.weekday() in (4, 5) and room.get("weekend_rate"):
        return float(room["weekend_rate"])
    return float(room["rate"])


class BookingSystem:
    """What Marina needs from wherever the bookings live."""

    name = "base"

    def availability(self, check_in, check_out, guests):
        raise NotImplementedError

    def create_booking(self, room_type, check_in, check_out, guests, first_name,
                       last_name, email="", phone="", channel="", status="requested"):
        raise NotImplementedError

    def get_booking(self, ref):
        raise NotImplementedError

    def modify_booking(self, ref, check_in=None, check_out=None, guests=None,
                       room_type=None):
        raise NotImplementedError

    def cancel_booking(self, ref):
        raise NotImplementedError

    def activity_slots(self, day, activity_id=None):
        raise NotImplementedError

    def book_activity(self, ref, activity_id, day, time, people):
        raise NotImplementedError

    def cancel_activity(self, ref, booking_id):
        raise NotImplementedError

    def add_handoff(self, channel, reason, contact="", booking_ref="", summary=""):
        raise NotImplementedError


_SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    ref TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    room_type TEXT NOT NULL,
    check_in TEXT NOT NULL,
    check_out TEXT NOT NULL,
    guests INTEGER NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    total REAL NOT NULL,
    currency TEXT NOT NULL,
    channel TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_bookings (
    id TEXT PRIMARY KEY,
    ref TEXT NOT NULL REFERENCES bookings(ref),
    activity_id TEXT NOT NULL,
    day TEXT NOT NULL,
    time TEXT NOT NULL,
    people INTEGER NOT NULL,
    total REAL NOT NULL,
    status TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoffs (
    id TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    channel TEXT,
    reason TEXT NOT NULL,
    contact TEXT,
    booking_ref TEXT,
    summary TEXT,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    kind TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    booking_ref TEXT,
    status TEXT NOT NULL
);
"""


class LocalBookingSystem(BookingSystem):
    """Bookings in a SQLite file, with rooms and activities from hotel.yaml."""

    name = "local"

    def __init__(self, path=None):
        cfg = (load_config().get("hotel") or {})
        self.path = str(path or resolve_data(cfg.get("database", "hotel.db")))
        self._lock = threading.RLock()
        with self._db() as db:
            db.executescript(_SCHEMA)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # Rooms

    def _check_dates(self, check_in, check_out):
        ci, co = _day(check_in, "arrival date"), _day(check_out, "departure date")
        today = profile.now().date()
        if ci < today:
            raise BookingError("That arrival date is in the past.")
        if co <= ci:
            raise BookingError("The departure date has to be after the arrival date.")
        if (co - ci).days > MAX_NIGHTS:
            raise BookingError(f"Stays longer than {MAX_NIGHTS} nights need a "
                               "member of staff.")
        if (ci - today).days > MAX_DAYS_AHEAD:
            raise BookingError("That is further ahead than we take bookings for.")
        return ci, co

    def _booked(self, db, room_type, night, exclude_ref=None):
        row = db.execute(
            "SELECT COUNT(*) FROM bookings WHERE room_type = ? AND status IN (?, ?) "
            "AND check_in <= ? AND check_out > ? AND ref IS NOT ?",
            (room_type, *HOLDS_ROOM, night.isoformat(), night.isoformat(), exclude_ref),
        ).fetchone()
        return row[0]

    def _rooms_left(self, db, room, ci, co, exclude_ref=None):
        return min(int(room.get("count", 0)) - self._booked(db, room["id"], n, exclude_ref)
                   for n in _nights(ci, co))

    def _quote(self, room, ci, co):
        return round(sum(nightly_rate(room, n) for n in _nights(ci, co)), 2)

    def availability(self, check_in, check_out, guests):
        ci, co = self._check_dates(check_in, check_out)
        guests = int(guests or 1)
        out = []
        with self._lock, self._db() as db:
            for room in profile.room_types().values():
                if guests > int(room.get("max_guests", 2)):
                    continue
                left = self._rooms_left(db, room, ci, co)
                if left <= 0:
                    continue
                total = self._quote(room, ci, co)
                out.append({
                    "room_type": room["id"],
                    "name": room["name"],
                    "rooms_left": left,
                    "nights": (co - ci).days,
                    "total": total,
                    "currency": profile.currency(),
                })
        return out

    def create_booking(self, room_type, check_in, check_out, guests, first_name,
                       last_name, email="", phone="", channel="", status="requested"):
        room = profile.room_types().get(room_type)
        if not room:
            raise BookingError("I don't know that room type.")
        ci, co = self._check_dates(check_in, check_out)
        guests = int(guests or 1)
        if guests > int(room.get("max_guests", 2)):
            raise BookingError(f"The {room['name']} sleeps up to "
                               f"{room.get('max_guests', 2)}.")
        first_name, last_name = (first_name or "").strip(), (last_name or "").strip()
        if not first_name or not last_name:
            raise BookingError("I need the guest's first and last name.")
        if not (email or "").strip() and not (phone or "").strip():
            raise BookingError("I need an email address or a phone number for the booking.")

        with self._lock, self._db() as db:
            if self._rooms_left(db, room, ci, co) <= 0:
                raise BookingError(f"The {room['name']} has just gone for those dates.")
            ref = new_ref()
            while db.execute("SELECT 1 FROM bookings WHERE ref = ?", (ref,)).fetchone():
                ref = new_ref()
            now = _now_iso()
            total = self._quote(room, ci, co)
            db.execute(
                "INSERT INTO bookings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ref, status, room_type, ci.isoformat(), co.isoformat(), guests,
                 first_name, last_name, (email or "").strip(), (phone or "").strip(),
                 total, profile.currency(), channel, now, now))
        return self.get_booking(ref)

    def get_booking(self, ref):
        ref = normalise_ref(ref)
        with self._lock, self._db() as db:
            row = db.execute("SELECT * FROM bookings WHERE ref = ?", (ref,)).fetchone()
            if not row:
                return None
            acts = db.execute(
                "SELECT * FROM activity_bookings WHERE ref = ? AND status = 'booked' "
                "ORDER BY day, time", (ref,)).fetchall()
        out = dict(row)
        room = profile.room_types().get(out["room_type"]) or {}
        out["room_name"] = room.get("name", out["room_type"])
        out["activities"] = [dict(a) for a in acts]
        return out

    def modify_booking(self, ref, check_in=None, check_out=None, guests=None,
                       room_type=None):
        current = self.get_booking(ref)
        if not current:
            raise BookingError("I can't find that booking.")
        if current["status"] not in HOLDS_ROOM:
            raise BookingError("That booking has been cancelled.")
        room_type = room_type or current["room_type"]
        room = profile.room_types().get(room_type)
        if not room:
            raise BookingError("I don't know that room type.")
        ci, co = self._check_dates(check_in or current["check_in"],
                                   check_out or current["check_out"])
        guests = int(guests or current["guests"])
        if guests > int(room.get("max_guests", 2)):
            raise BookingError(f"The {room['name']} sleeps up to "
                               f"{room.get('max_guests', 2)}.")
        with self._lock, self._db() as db:
            if self._rooms_left(db, room, ci, co, exclude_ref=current["ref"]) <= 0:
                raise BookingError(f"The {room['name']} isn't free for those dates.")
            db.execute(
                "UPDATE bookings SET room_type=?, check_in=?, check_out=?, guests=?, "
                "total=?, updated=? WHERE ref=?",
                (room_type, ci.isoformat(), co.isoformat(), guests,
                 self._quote(room, ci, co), _now_iso(), current["ref"]))
        return self.get_booking(current["ref"])

    def cancel_booking(self, ref):
        current = self.get_booking(ref)
        if not current:
            raise BookingError("I can't find that booking.")
        if current["status"] == "cancelled":
            raise BookingError("That booking is already cancelled.")
        arrival = datetime.combine(date.fromisoformat(current["check_in"]),
                                   datetime.min.time(), tzinfo=profile.tz())
        hours_left = (arrival - profile.now()).total_seconds() / 3600
        free = hours_left >= profile.free_cancellation_hours()
        with self._lock, self._db() as db:
            db.execute("UPDATE bookings SET status='cancelled', updated=? WHERE ref=?",
                       (_now_iso(), current["ref"]))
            db.execute("UPDATE activity_bookings SET status='cancelled' WHERE ref=?",
                       (current["ref"],))
        out = self.get_booking(current["ref"])
        out["free_cancellation"] = free
        return out

    # Activities

    def _taken(self, db, activity_id, day, time):
        row = db.execute(
            "SELECT COALESCE(SUM(people), 0) FROM activity_bookings WHERE "
            "activity_id=? AND day=? AND time=? AND status='booked'",
            (activity_id, day.isoformat(), time)).fetchone()
        return int(row[0])

    def activity_slots(self, day, activity_id=None):
        d = _day(day, "date")
        if d < profile.now().date():
            raise BookingError("That date is in the past.")
        weekday = d.strftime("%a").lower()
        acts = profile.activities()
        if activity_id:
            if activity_id not in acts:
                raise BookingError("I don't know that activity.")
            acts = {activity_id: acts[activity_id]}
        out = []
        with self._lock, self._db() as db:
            for a in acts.values():
                days = [x.lower()[:3] for x in a.get("days") or []]
                if days and weekday not in days:
                    continue
                for t in a.get("times") or []:
                    left = int(a.get("capacity", 0)) - self._taken(db, a["id"], d, t)
                    if left > 0:
                        out.append({"activity_id": a["id"], "name": a["name"],
                                    "date": d.isoformat(), "time": t,
                                    "spaces_left": left, "price": a.get("price"),
                                    "currency": profile.currency(),
                                    "provider": a.get("provider")})
        return out

    def book_activity(self, ref, activity_id, day, time, people):
        booking = self.get_booking(ref)
        if not booking or booking["status"] not in HOLDS_ROOM:
            raise BookingError("Activities are booked against a current room booking.")
        a = profile.activities().get(activity_id)
        if not a:
            raise BookingError("I don't know that activity.")
        d = _day(day, "date")
        if not (date.fromisoformat(booking["check_in"]) <= d
                <= date.fromisoformat(booking["check_out"])):
            raise BookingError("That date isn't during the guest's stay.")
        people = int(people or 1)
        if people < 1:
            raise BookingError("At least one person has to go.")
        time = str(time).strip()
        if time not in (a.get("times") or []):
            raise BookingError(f"The {a['name']} runs at {', '.join(a.get('times') or [])}.")
        with self._lock, self._db() as db:
            weekday = d.strftime("%a").lower()
            days = [x.lower()[:3] for x in a.get("days") or []]
            if days and weekday not in days:
                raise BookingError(f"The {a['name']} doesn't run that day.")
            left = int(a.get("capacity", 0)) - self._taken(db, activity_id, d, time)
            if people > left:
                raise BookingError(f"Only {max(left, 0)} spaces are left at {time}.")
            booking_id = "A" + new_ref()
            total = round(float(a.get("price", 0)) * people, 2)
            db.execute("INSERT INTO activity_bookings VALUES (?,?,?,?,?,?,?,?,?)",
                       (booking_id, booking["ref"], activity_id, d.isoformat(), time,
                        people, total, "booked", _now_iso()))
        return {"id": booking_id, "activity": a["name"], "date": d.isoformat(),
                "time": time, "people": people, "total": total,
                "currency": profile.currency(), "provider": a.get("provider")}

    def cancel_activity(self, ref, booking_id):
        with self._lock, self._db() as db:
            cur = db.execute(
                "UPDATE activity_bookings SET status='cancelled' WHERE id=? AND ref=? "
                "AND status='booked'", (str(booking_id).strip().upper(), normalise_ref(ref)))
            if cur.rowcount == 0:
                raise BookingError("I can't find that activity on this booking.")
        return True

    # Staff

    def add_handoff(self, channel, reason, contact="", booking_ref="", summary=""):
        hid = "H" + new_ref()
        with self._lock, self._db() as db:
            db.execute("INSERT INTO handoffs VALUES (?,?,?,?,?,?,?,?)",
                       (hid, _now_iso(), channel, reason, contact,
                        normalise_ref(booking_ref) if booking_ref else "",
                        summary, "open"))
        return hid

    # Messages to guests

    def add_message(self, kind, recipient, subject, body, booking_ref, status):
        mid = "M" + new_ref()
        with self._lock, self._db() as db:
            db.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?)",
                       (mid, _now_iso(), kind, recipient, subject, body,
                        normalise_ref(booking_ref), status))
        return mid

    def find_for_resend(self, last_name, email):
        """The guest's current booking, matched on surname and the email
        already on it, so a confirmation can only go back to that address."""
        with self._lock, self._db() as db:
            row = db.execute(
                "SELECT ref FROM bookings WHERE lower(last_name) = lower(?) AND "
                "lower(email) = lower(?) AND status IN (?, ?) ORDER BY created DESC",
                (str(last_name or "").strip(), str(email or "").strip(), *HOLDS_ROOM)).fetchone()
        return self.get_booking(row["ref"]) if row else None

    # For the staff admin tool, not for Marina.

    def list(self, table, where="1=1", params=()):
        assert table in ("bookings", "activity_bookings", "handoffs", "outbox")
        with self._lock, self._db() as db:
            return [dict(r) for r in db.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY rowid", params)]

    def set_status(self, table, key, value, status):
        assert table in ("bookings", "handoffs")
        column = "ref" if table == "bookings" else "id"
        with self._lock, self._db() as db:
            return db.execute(f"UPDATE {table} SET status=? WHERE {column}=?",
                              (status, value)).rowcount

    def erase_guest(self, ref):
        """Remove a guest's personal details but keep the stay itself, which
        the hotel may need to keep for its accounts."""
        ref = normalise_ref(ref)
        with self._lock, self._db() as db:
            n = db.execute(
                "UPDATE bookings SET first_name='(erased)', last_name='(erased)', "
                "email='', phone='', updated=? WHERE ref=?", (_now_iso(), ref)).rowcount
            db.execute("UPDATE handoffs SET contact='', summary='(erased)' "
                       "WHERE booking_ref=?", (ref,))
        return n


_system = None
_system_lock = threading.Lock()


def booking_system():
    """The configured booking system. `hotel.booking_system: local` is the
    built-in store; a PMS adapter is a BookingSystem subclass registered
    here under its own name."""
    global _system
    with _system_lock:
        if _system is None:
            kind = ((load_config().get("hotel") or {}).get("booking_system") or "local")
            if kind != "local":
                raise RuntimeError(
                    f"hotel.booking_system is {kind!r}, but only 'local' ships "
                    "with Marina. A PMS adapter implements BookingSystem in "
                    "server/process/hotel/store.py.")
            _system = LocalBookingSystem()
        return _system
