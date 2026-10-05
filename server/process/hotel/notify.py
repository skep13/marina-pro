"""Booking confirmations, sent to the guest instead of read out.

A reference read aloud at a busy reception can be overheard, and with the
surname it is enough to change someone's stay. So Marina never says it: the
confirmation, with the reference, goes to the email or phone number the guest
gave, and every message is listed in the outbox on the staff dashboard.

Email goes out through the hotel's own mail server when `hotel.email` has an
`smtp_host`. Without one, messages are queued in the outbox and marked as not
sent, so nothing claims to have been delivered when it wasn't. Text messages
need an SMS provider and are always queued for now.
"""
import smtplib
from datetime import date
from email.message import EmailMessage

from process.config import load_config
from process.hotel import profile
from process.hotel.store import booking_system


def mask_email(address):
    """a•••@example.com: enough for the guest to recognise it."""
    name, _, domain = str(address or "").partition("@")
    if not domain:
        return "the address you gave"
    return f"{name[:1]}•••@{domain}"


def mask_phone(number):
    digits = "".join(c for c in str(number or "") if c.isdigit())
    return f"the number ending {digits[-3:]}" if len(digits) >= 3 else "your phone"


def _when(iso):
    d = date.fromisoformat(iso)
    return f"{d:%A} {d.day} {d:%B %Y}"


def _body(b):
    hotel = profile.load()
    cur = b.get("currency") or profile.currency()
    lines = [
        f"Thank you for booking with {hotel.get('name', 'us')}.",
        "",
        f"Booking reference: {b['ref']}",
        f"Name: {b['first_name']} {b['last_name']}",
        f"Room: {b['room_name']}",
        f"Arriving: {_when(b['check_in'])} (check-in from {hotel.get('check_in', '15:00')})",
        f"Leaving: {_when(b['check_out'])} (check-out by {hotel.get('check_out', '11:00')})",
        f"Guests: {b['guests']}",
        f"Total: {b['total']:.2f} {cur}",
        "",
        "Your room is held until you pay through the secure payment link, "
        "which follows separately. We never ask for card details by phone or email.",
        "",
        "Keep this reference private: with your surname, it lets you change or "
        "cancel the booking.",
    ]
    if hotel.get("phone"):
        lines += ["", f"Questions? Call us on {hotel['phone']}."]
    return "\n".join(lines)


def _smtp(to, subject, body):
    cfg = ((load_config().get("hotel") or {}).get("email") or {})
    if not cfg.get("smtp_host"):
        return False
    msg = EmailMessage()
    msg["From"] = cfg.get("from") or cfg.get("username") or profile.load().get("email", "")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port", 587)), timeout=15) as s:
        s.starttls()
        if cfg.get("username"):
            s.login(cfg["username"], cfg.get("password", ""))
        s.send_message(msg)
    return True


def send_confirmation(b):
    """Send or queue the confirmation for booking `b`. Returns how to describe
    where it went, without the address itself."""
    subject = f"Your booking at {profile.load().get('name', 'the hotel')}: {b['ref']}"
    body = _body(b)
    sys = booking_system()
    if b.get("email"):
        try:
            status = "sent" if _smtp(b["email"], subject, body) else "queued"
        except Exception as e:
            print(f"[hotel] email failed: {type(e).__name__}", flush=True)
            status = "failed"
        sys.add_message("email", b["email"], subject, body, b["ref"], status)
        return {"sent_to": mask_email(b["email"]), "by": "email", "status": status}
    sys.add_message("sms", b["phone"], subject, body, b["ref"], "queued")
    return {"sent_to": mask_phone(b["phone"]), "by": "text", "status": "queued"}
