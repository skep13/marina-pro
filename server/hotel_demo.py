"""Reset the hotel's bookings for a demo and add a few realistic ones.

    python server/hotel_demo.py

Clears every booking, activity and staff request in the configured database,
then books a handful of stays around today so the front desk dashboard isn't
empty. One of them is always QX7M4T for Sarah Collins, arriving tomorrow for
three nights. The app's menu bar icon has the same thing as "Reset demo
bookings".
"""
from process.hotel import demo
from process.hotel.store import booking_system

if __name__ == "__main__":
    n = demo.reset()
    print(f"Reset {booking_system().path}: {n} bookings, including QX7M4T for Sarah Collins.")
