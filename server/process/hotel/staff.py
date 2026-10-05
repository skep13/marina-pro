"""The front desk dashboard: what Marina has booked and what she has passed
to staff, refreshing every few seconds. Open /staff in a browser beside her.

Guests' details are on it, so it only answers requests made on this machine
directly. Set hotel.staff_key to reach it any other way, for example through
`tailscale serve`, and open /staff?key=... instead.
"""
import hmac
from datetime import date, timedelta

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from process.config import load_config
from process.hotel import profile
from process.hotel.store import booking_system

_FORWARDED = ("x-forwarded-for", "forwarded", "tailscale-user-login", "x-real-ip")


def _check(request: Request):
    key = str((load_config().get("hotel") or {}).get("staff_key") or "")
    given = request.query_params.get("key") or request.headers.get("x-staff-key") or ""
    if key and hmac.compare_digest(given, key):
        return
    host = request.client.host if request.client else ""
    direct = host in ("127.0.0.1", "::1") and not any(h in request.headers for h in _FORWARDED)
    if not direct:
        raise HTTPException(403, "The staff page is only open on the front desk computer.")


def _snapshot():
    sys = booking_system()
    today = profile.now().date()
    rooms = profile.room_types()
    bookings = sys.list("bookings")
    live = [b for b in bookings if b["status"] in ("requested", "confirmed")]
    tonight = sum(1 for b in live
                  if date.fromisoformat(b["check_in"]) <= today < date.fromisoformat(b["check_out"]))
    total_rooms = sum(int(r.get("count", 0)) for r in rooms.values())
    acts = profile.activities()
    activity_rows = []
    for a in sys.list("activity_bookings"):
        a["activity"] = acts.get(a["activity_id"], {}).get("name", a["activity_id"])
        activity_rows.append(a)
    for b in bookings:
        b["room_name"] = rooms.get(b["room_type"], {}).get("name", b["room_type"])
    return {
        "hotel": profile.load().get("name", "Hotel"),
        "currency": profile.currency(),
        "today": today.isoformat(),
        "tonight": tonight,
        "rooms": total_rooms,
        "arrivals_today": sum(1 for b in live if b["check_in"] == today.isoformat()),
        "arrivals_week": sum(1 for b in live if today.isoformat() <= b["check_in"]
                             <= (today + timedelta(days=7)).isoformat()),
        "bookings": list(reversed(bookings))[:50],
        "activities": list(reversed(activity_rows))[:50],
        "handoffs": list(reversed(sys.list("handoffs")))[:50],
    }


def mount(app):
    @app.get("/staff", include_in_schema=False)
    def staff_page(request: Request):
        _check(request)
        return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store"})

    @app.get("/staff/data")
    def staff_data(request: Request):
        _check(request)
        return _snapshot()

    @app.post("/staff/demo-reset")
    def staff_demo_reset(request: Request):
        """Clear the bookings and load the example stays, for a demo."""
        _check(request)
        from process.hotel import demo
        return {"ok": True, "bookings": demo.reset()}

    @app.post("/staff/handoffs/{hid}/done")
    def staff_done(hid: str, request: Request):
        _check(request)
        return {"ok": bool(booking_system().set_status("handoffs", hid, hid, "done"))}


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Front desk</title>
<style>
:root {
  --bg: #f4f5f7; --panel: #ffffff; --text: #1c2230; --muted: #6a7385;
  --line: #e3e6ec; --accent: #1f6feb; --new: #fff6d6; --open: #b42318;
  --ok: #1a7f37; --pill: #eef1f6;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12151b; --panel: #1a1e26; --text: #e8ecf3; --muted: #9aa3b4;
    --line: #2a303b; --accent: #6aa3ff; --new: #3a3320; --open: #ff7b72;
    --ok: #56d364; --pill: #242a35;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
header { padding: 20px 28px 8px; display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; }
h1 { font-size: 20px; margin: 0; font-weight: 650; }
.sub { color: var(--muted); }
.live { margin-left: auto; color: var(--muted); font-size: 12px; }
.live::before { content: ""; display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: var(--ok); margin-right: 6px; vertical-align: 1px; }
main { padding: 8px 28px 32px; display: grid; gap: 18px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
.card .n { font-size: 26px; font-weight: 650; }
.card .l { color: var(--muted); font-size: 12px; }
section { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
section h2 { font-size: 14px; margin: 0; padding: 12px 16px; border-bottom: 1px solid var(--line); }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 9px 16px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { color: var(--muted); font-weight: 500; font-size: 12px; }
tr:last-child td { border-bottom: 0; }
tr.new td { background: var(--new); transition: background 3s ease; }
.ref { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .04em; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 99px; background: var(--pill); font-size: 12px; }
.pill.open { color: var(--open); }
.pill.cancelled { color: var(--muted); text-decoration: line-through; }
.empty { padding: 14px 16px; color: var(--muted); }
button { font: inherit; padding: 3px 10px; border-radius: 7px; border: 1px solid var(--line);
  background: var(--panel); color: var(--text); cursor: pointer; }
button:hover { border-color: var(--accent); }
.wrap { white-space: normal; min-width: 220px; }
</style>
</head>
<body>
<header>
  <h1 id="hotel">Front desk</h1>
  <span class="sub" id="today"></span>
  <span class="live">Live from Marina</span>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="n" id="tonight">–</div><div class="l">Rooms booked tonight</div></div>
    <div class="card"><div class="n" id="arrivals">–</div><div class="l">Arrivals today</div></div>
    <div class="card"><div class="n" id="week">–</div><div class="l">Arrivals in the next 7 days</div></div>
    <div class="card"><div class="n" id="open">–</div><div class="l">Waiting for staff</div></div>
  </div>
  <section><h2>Waiting for staff</h2><div class="scroll" id="handoffs"></div></section>
  <section><h2>Bookings</h2><div class="scroll" id="bookings"></div></section>
  <section><h2>Activities</h2><div class="scroll" id="activities"></div></section>
</main>
<script>
const seen = new Set();
let first = true;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const day = (iso) => new Date(iso + 'T12:00:00').toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
const time = (iso) => new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
const money = (n, cur) => new Intl.NumberFormat(undefined, { style: 'currency', currency: cur }).format(n);
const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : '–');
const channel = (c) => ({ reception: 'Reception', phone: 'Phone' }[c] || esc(cap(c)));

function rows(list, key, cols, empty) {
  if (!list.length) return `<div class="empty">${empty}</div>`;
  const head = cols.map((c) => `<th>${c[0]}</th>`).join('');
  const body = list.map((r) => {
    const k = key + ':' + r[key];
    const fresh = !first && !seen.has(k);
    seen.add(k);
    return `<tr class="${fresh ? 'new' : ''}">${cols.map((c) => `<td class="${c[2] || ''}">${c[1](r)}</td>`).join('')}</tr>`;
  }).join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

async function done(id) {
  await fetch(`/staff/handoffs/${encodeURIComponent(id)}/done${location.search}`, { method: 'POST' });
  refresh();
}

async function refresh() {
  let d;
  try { d = await (await fetch('/staff/data' + location.search)).json(); } catch { return; }
  document.getElementById('hotel').textContent = d.hotel + ' · Front desk';
  document.getElementById('today').textContent = new Date(d.today + 'T12:00:00').toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' });
  document.getElementById('tonight').textContent = `${d.tonight} / ${d.rooms}`;
  document.getElementById('arrivals').textContent = d.arrivals_today;
  document.getElementById('week').textContent = d.arrivals_week;
  const open = d.handoffs.filter((h) => h.status === 'open');
  document.getElementById('open').textContent = open.length;
  document.getElementById('handoffs').innerHTML = rows(d.handoffs, 'id', [
    ['When', (h) => time(h.created)],
    ['From', (h) => channel(h.channel)],
    ['Reason', (h) => esc(h.reason), 'wrap'],
    ['Details', (h) => esc(h.summary || '–'), 'wrap'],
    ['Contact', (h) => esc(h.contact || '–')],
    ['Booking', (h) => `<span class="ref">${esc(h.booking_ref || '–')}</span>`],
    ['', (h) => h.status === 'open'
      ? `<button onclick="done('${esc(h.id)}')">Mark done</button>` : '<span class="pill">Done</span>'],
  ], 'Nothing waiting.');
  document.getElementById('bookings').innerHTML = rows(d.bookings, 'ref', [
    ['Ref', (b) => `<span class="ref">${esc(b.ref)}</span>`],
    ['Guest', (b) => esc(`${b.first_name} ${b.last_name}`)],
    ['Room', (b) => esc(b.room_name)],
    ['Stay', (b) => `${day(b.check_in)} – ${day(b.check_out)}`],
    ['Guests', (b) => esc(b.guests)],
    ['Total', (b) => money(b.total, b.currency)],
    ['Via', (b) => channel(b.channel)],
    ['Status', (b) => `<span class="pill ${esc(b.status)}">${b.status === 'requested' ? 'Awaiting payment' : esc(cap(b.status))}</span>`],
    ['Made', (b) => time(b.created)],
  ], 'No bookings yet.');
  document.getElementById('activities').innerHTML = rows(d.activities, 'id', [
    ['Activity', (a) => esc(a.activity)],
    ['When', (a) => `${day(a.day)} ${esc(a.time)}`],
    ['People', (a) => esc(a.people)],
    ['Total', (a) => money(a.total, d.currency)],
    ['Booking', (a) => `<span class="ref">${esc(a.ref)}</span>`],
    ['Status', (a) => `<span class="pill ${esc(a.status)}">${esc(cap(a.status))}</span>`],
  ], 'No activities booked yet.');
  first = false;
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""
