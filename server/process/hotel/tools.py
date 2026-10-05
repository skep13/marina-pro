"""What Marina can do for a hotel's guests: rooms, activities, and passing a
guest to a member of staff.

Each tool returns a short JSON string for the model. Anything that touches an
existing booking needs the booking reference and the surname on it, so a
reference overheard at reception isn't enough to change someone's stay.
"""
import functools
import json
from datetime import datetime, timedelta, timezone

from process import config
from process.config import load_config
from process.hotel import notify, profile
from process.hotel.store import BookingError, booking_system, normalise_ref


def _ok(**data):
    return json.dumps({"ok": True, **data})


def _err(message):
    return json.dumps({"ok": False, "error": message})


def _channel():
    """Which desk the guest is talking to: the profile's name, e.g.
    'reception' or 'phone'. Stored on bookings and handoffs."""
    return config.preset()


def _room_id(value):
    rooms = profile.room_types()
    text = str(value or "").strip().lower()
    if text in rooms:
        return text
    for rid, r in rooms.items():
        if text and (text in r["name"].lower() or r["name"].lower() in text):
            return rid
    return text


def _activity_id(value):
    acts = profile.activities()
    text = str(value or "").strip().lower()
    if text in acts:
        return text
    for aid, a in acts.items():
        if text and (text in a["name"].lower() or a["name"].lower() in text):
            return aid
    return text


def _verified(reference, last_name):
    booking = booking_system().get_booking(reference)
    if not booking:
        raise BookingError("I can't find a booking with that reference.")
    if str(last_name or "").strip().lower() != booking["last_name"].lower():
        raise BookingError("That surname doesn't match the booking.")
    return booking


def _summary(b):
    return {
        "reference": b["ref"],
        "status": b["status"],
        "room": b["room_name"],
        "check_in": b["check_in"],
        "check_out": b["check_out"],
        "guests": b["guests"],
        "name": f"{b['first_name']} {b['last_name']}",
        "total": b["total"],
        "currency": b["currency"],
        "activities": [
            {"id": a["id"], "activity": profile.activities().get(a["activity_id"], {})
             .get("name", a["activity_id"]), "date": a["day"], "time": a["time"],
             "people": a["people"]}
            for a in b.get("activities") or []
        ],
    }


def _guarded(fn):
    @functools.wraps(fn)
    def run(*args, **kw):
        try:
            return fn(*args, **kw)
        except BookingError as e:
            return _err(str(e))
    return run


@_guarded
def check_availability(check_in, check_out, guests=1):
    rooms = booking_system().availability(check_in, check_out, guests)
    if not rooms:
        return _ok(rooms=[], note="Nothing is free for those dates and that many guests.")
    return _ok(rooms=rooms)


def _recent_bookings(minutes):
    """Bookings Marina has taken on this desk lately. Website and staff
    bookings don't count towards her limit."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    return len(booking_system().list("bookings", "created >= ? AND channel = ?",
                                     (since, _channel())))


@_guarded
def book_room(room_type, check_in, check_out, guests, first_name, last_name,
              email="", phone=""):
    limit = int((load_config().get("hotel") or {}).get("max_bookings_per_10_minutes", 5))
    if limit and _recent_bookings(10) >= limit:
        raise BookingError("I can't take any more bookings just now, so I'll pass "
                           "you to a member of staff.")
    b = booking_system().create_booking(
        _room_id(room_type), check_in, check_out, guests, first_name, last_name,
        email=email, phone=phone, channel=_channel())
    sent = notify.send_confirmation(b)
    # The reference is left out on purpose: it goes to the guest privately.
    out = {k: v for k, v in _summary(b).items() if k != "reference"}
    return _ok(
        **out, confirmation=sent,
        next_step=(f"Tell the guest the room is held, and that their confirmation "
                   f"with the booking reference is on its way to them by "
                   f"{sent['by']}. Never read a booking reference out loud. The "
                   "secure payment link follows separately; never take card details."))


@_guarded
def resend_confirmation(last_name, email):
    b = booking_system().find_for_resend(last_name, email)
    if not b:
        raise BookingError("I couldn't find a current booking with that surname "
                           "and email address.")
    sent = notify.send_confirmation(b)
    return _ok(confirmation=sent,
               note="Sent to the address on the booking. Don't read the reference out.")


@_guarded
def find_booking(reference, last_name):
    return _ok(**_summary(_verified(reference, last_name)))


@_guarded
def change_booking(reference, last_name, check_in=None, check_out=None,
                   guests=None, room_type=None):
    b = _verified(reference, last_name)
    out = booking_system().modify_booking(
        b["ref"], check_in=check_in, check_out=check_out, guests=guests,
        room_type=_room_id(room_type) if room_type else None)
    return _ok(**_summary(out), previous_total=b["total"])


@_guarded
def cancel_booking(reference, last_name):
    b = _verified(reference, last_name)
    out = booking_system().cancel_booking(b["ref"])
    policy = (profile.load().get("policies") or {}).get("cancellation", "")
    return _ok(**_summary(out), free_cancellation=out["free_cancellation"],
               cancellation_policy=" ".join(str(policy).split()))


@_guarded
def list_activities(date, activity=None):
    slots = booking_system().activity_slots(
        date, _activity_id(activity) if activity else None)
    if not slots:
        return _ok(slots=[], note="Nothing with spaces left that day.")
    return _ok(slots=slots)


@_guarded
def book_activity(reference, last_name, activity, date, time, people=1):
    b = _verified(reference, last_name)
    out = booking_system().book_activity(b["ref"], _activity_id(activity), date,
                                         time, people)
    return _ok(**out)


@_guarded
def cancel_activity(reference, last_name, activity_booking_id):
    b = _verified(reference, last_name)
    booking_system().cancel_activity(b["ref"], activity_booking_id)
    return _ok(cancelled=str(activity_booking_id).upper())


@_guarded
def hand_over_to_staff(reason, contact="", reference="", summary=""):
    hid = booking_system().add_handoff(
        _channel(), reason, contact=contact,
        booking_ref=normalise_ref(reference) if reference else "", summary=summary)
    return _ok(handoff=hid, note=("Passed to the front desk team, who will pick "
                                  "it up. Tell the guest what happens next."))


_DATE = {"type": "string", "description": "A date as YYYY-MM-DD."}
_REF = {"type": "string", "description": "The six-character booking reference."}
_SURNAME = {"type": "string", "description": "The surname on the booking."}

SPECS = [
    ("check_availability", check_availability, {
        "description": "Rooms free for a stay and the total price. Use before "
                       "quoting any price or offering to book.",
        "parameters": {"type": "object", "properties": {
            "check_in": _DATE, "check_out": _DATE,
            "guests": {"type": "integer"}},
            "required": ["check_in", "check_out", "guests"]}}),
    ("book_room", book_room, {
        "description": "Book a room once the guest has confirmed the room, "
                       "dates, guests, their name and an email or phone number.",
        "parameters": {"type": "object", "properties": {
            "room_type": {"type": "string", "description": "Room type id or name."},
            "check_in": _DATE, "check_out": _DATE,
            "guests": {"type": "integer"},
            "first_name": {"type": "string"}, "last_name": {"type": "string"},
            "email": {"type": "string"}, "phone": {"type": "string"}},
            "required": ["room_type", "check_in", "check_out", "guests",
                         "first_name", "last_name"]}}),
    ("resend_confirmation", resend_confirmation, {
        "description": "Send a guest's confirmation, with their booking "
                       "reference, again. Only goes to the email already on "
                       "the booking.",
        "parameters": {"type": "object", "properties": {
            "last_name": _SURNAME,
            "email": {"type": "string", "description": "The email address on the booking."}},
            "required": ["last_name", "email"]}}),
    ("find_booking", find_booking, {
        "description": "Look up a booking. Needs the reference and the surname.",
        "parameters": {"type": "object", "properties": {
            "reference": _REF, "last_name": _SURNAME},
            "required": ["reference", "last_name"]}}),
    ("change_booking", change_booking, {
        "description": "Change the dates, guests or room of a booking. Only "
                       "pass what changes.",
        "parameters": {"type": "object", "properties": {
            "reference": _REF, "last_name": _SURNAME,
            "check_in": _DATE, "check_out": _DATE,
            "guests": {"type": "integer"},
            "room_type": {"type": "string"}},
            "required": ["reference", "last_name"]}}),
    ("cancel_booking", cancel_booking, {
        "description": "Cancel a booking, after the guest has confirmed they "
                       "want to and heard the cancellation policy.",
        "parameters": {"type": "object", "properties": {
            "reference": _REF, "last_name": _SURNAME},
            "required": ["reference", "last_name"]}}),
    ("list_activities", list_activities, {
        "description": "Activity times with spaces left on a date.",
        "parameters": {"type": "object", "properties": {
            "date": _DATE,
            "activity": {"type": "string", "description": "Optional: one activity."}},
            "required": ["date"]}}),
    ("book_activity", book_activity, {
        "description": "Book an activity for a guest with a room booking.",
        "parameters": {"type": "object", "properties": {
            "reference": _REF, "last_name": _SURNAME,
            "activity": {"type": "string"}, "date": _DATE,
            "time": {"type": "string", "description": "HH:MM"},
            "people": {"type": "integer"}},
            "required": ["reference", "last_name", "activity", "date", "time",
                         "people"]}}),
    ("cancel_activity", cancel_activity, {
        "description": "Cancel one activity booking.",
        "parameters": {"type": "object", "properties": {
            "reference": _REF, "last_name": _SURNAME,
            "activity_booking_id": {"type": "string"}},
            "required": ["reference", "last_name", "activity_booking_id"]}}),
    ("hand_over_to_staff", hand_over_to_staff, {
        "description": "Pass the guest to a member of staff: complaints, "
                       "anything you can't do, or when they ask for a person.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string"},
            "contact": {"type": "string", "description": "Phone or email to call back on."},
            "reference": _REF,
            "summary": {"type": "string", "description": "What staff need to know."}},
            "required": ["reason"]}}),
]
