import os
import uuid
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

load_dotenv()

app = FastAPI(title="Flight Management API")
engine = create_engine(
    os.getenv("DATABASE_URL"),
    pool_pre_ping=True,      # test each connection before using; reconnect if dead
    pool_recycle=300,        # refresh connections older than 5 minutes
)


# ---------- Role tiers (file requirement: super-admin vs ops-agent rights) ----------

VALID_ROLES = {"super_admin", "ops_agent"}
SUPER_ONLY = {"create_flight", "cancel_flight"}


def check_role(role: str | None, action: str) -> str:
    if role is None:
        raise HTTPException(401, "Missing X-Admin-Role header")
    if role not in VALID_ROLES:
        raise HTTPException(403, f"Unknown role '{role}'")
    if action in SUPER_ONLY and role != "super_admin":
        raise HTTPException(403,
            f"Action '{action}' requires super_admin; you are '{role}'")
    return role


@app.get("/health")
def health():
    with engine.connect() as conn:
        flight_count = conn.execute(text("SELECT count(*) FROM flights")).scalar()
    return {"status": "ok", "flights_in_db": flight_count}


# ---------- Schemas ----------

class SeatClassInput(BaseModel):
    class_name: str = Field(..., description="first / business / economy")
    total_seats: int
    base_price: float


class FlightCreate(BaseModel):
    flight_number: str
    origin: str
    destination: str
    origin_tz: str = "Europe/London"
    dest_tz: str = "Asia/Dubai"
    departure_ts: str          # ISO: "2026-09-15T05:00:00Z"
    arrival_ts: str
    total_capacity: int
    seat_classes: list[SeatClassInput]


class ScheduleEdit(BaseModel):
    new_departure_ts: str
    new_arrival_ts: str


class SeatClassAdjust(BaseModel):
    class_name: str
    new_total_seats: int


# ---------- Admin: create flight (super_admin only) ----------

@app.post("/admin/flights", status_code=201)
def create_flight(flight: FlightCreate,
                  x_admin_role: str | None = Header(default=None)):
    role = check_role(x_admin_role, "create_flight")

    for sc in flight.seat_classes:
        if sc.total_seats <= 0:
            raise HTTPException(422, f"Seat count for {sc.class_name} must be positive, got {sc.total_seats}")
        if sc.base_price <= 0:
            raise HTTPException(422, f"Price for {sc.class_name} must be positive")

    total = sum(sc.total_seats for sc in flight.seat_classes)
    if total != flight.total_capacity:
        raise HTTPException(422,
            f"Seat classes sum to {total} but capacity is {flight.total_capacity}")

    with engine.begin() as conn:
        dup = conn.execute(text("""
            SELECT id FROM flights
            WHERE flight_number = :fn
              AND departure_ts::date = (:dep)::date
        """), {"fn": flight.flight_number, "dep": flight.departure_ts}).first()
        if dup:
            raise HTTPException(409,
                f"Flight {flight.flight_number} already exists on that date")

        flight_id = conn.execute(text("""
            INSERT INTO flights (flight_number, origin, destination,
                                 origin_tz, destination_tz, departure_ts, arrival_ts,
                                 total_capacity, status)
            VALUES (:fn, :orig, :dest, :otz, :dtz, :dep, :arr, :cap, 'scheduled')
            RETURNING id
        """), {"fn": flight.flight_number, "orig": flight.origin,
               "dest": flight.destination, "otz": flight.origin_tz,
               "dtz": flight.dest_tz, "dep": flight.departure_ts,
               "arr": flight.arrival_ts, "cap": flight.total_capacity}).scalar()

        for sc in flight.seat_classes:
            conn.execute(text("""
                INSERT INTO seat_classes (flight_id, class, total_seats,
                                          available_seats, base_price)
                VALUES (:fid, :cls, :total, :total, :price)
            """), {"fid": flight_id, "cls": sc.class_name,
                   "total": sc.total_seats, "price": sc.base_price})

        conn.execute(text("""
            INSERT INTO audit_log (actor, actor_role, action, entity_type, entity_id, after_state)
            VALUES ('admin', :role, 'create_flight', 'flight', :fid, :after)
        """), {"role": role, "fid": flight_id,
               "after": f'{{"flight_number": "{flight.flight_number}", "capacity": {flight.total_capacity}, "status": "scheduled"}}'})

    return {"flight_id": flight_id, "message": f"Flight {flight.flight_number} created"}


# ---------- Admin: cancel flight (super_admin only) ----------

@app.post("/admin/flights/{flight_id}/cancel")
def cancel_flight(flight_id: int,
                  x_admin_role: str | None = Header(default=None)):
    role = check_role(x_admin_role, "cancel_flight")

    with engine.begin() as conn:
        flight = conn.execute(text("""
            SELECT id, flight_number, status FROM flights WHERE id = :fid
        """), {"fid": flight_id}).first()

        if not flight:
            raise HTTPException(404, f"Flight {flight_id} not found")
        if flight.status == 'cancelled':
            raise HTTPException(409, f"Flight {flight.flight_number} is already cancelled")

        before_status = flight.status

        conn.execute(text("""
            UPDATE flights SET status = 'cancelled', updated_at = now()
            WHERE id = :fid
        """), {"fid": flight_id})

        refunds_created = conn.execute(text("""
            INSERT INTO refunds (booking_id, amount, status, created_at)
            SELECT b.id, b.price_paid, 'pending', now()
            FROM bookings b
            WHERE b.flight_id = :fid AND b.status = 'confirmed'
            RETURNING id
        """), {"fid": flight_id}).fetchall()

        conn.execute(text("""
            INSERT INTO audit_log (actor, actor_role, action, entity_type, entity_id,
                                   before_state, after_state)
            VALUES ('admin', :role, 'cancel_flight', 'flight', :fid,
                    :before, :after)
        """), {"role": role, "fid": flight_id,
               "before": f'{{"status": "{before_status}"}}',
               "after": f'{{"status": "cancelled", "refunds_created": {len(refunds_created)}}}'})

    return {"flight_id": flight_id,
            "flight_number": flight.flight_number,
            "status": "cancelled",
            "refunds_created": len(refunds_created)}


# ---------- Admin: edit flight schedule (ops_agent allowed) ----------

@app.patch("/admin/flights/{flight_id}/schedule")
def edit_schedule(flight_id: int, edit: ScheduleEdit,
                  x_admin_role: str | None = Header(default=None)):
    role = check_role(x_admin_role, "edit_schedule")

    with engine.begin() as conn:
        flight = conn.execute(text("""
            SELECT id, flight_number, status, departure_ts, arrival_ts
            FROM flights WHERE id = :fid
        """), {"fid": flight_id}).first()

        if not flight:
            raise HTTPException(404, f"Flight {flight_id} not found")
        if flight.status == 'cancelled':
            raise HTTPException(409, "Cannot reschedule a cancelled flight")

        conn.execute(text("""
            UPDATE flights
            SET departure_ts = :dep, arrival_ts = :arr, updated_at = now()
            WHERE id = :fid
        """), {"dep": edit.new_departure_ts, "arr": edit.new_arrival_ts,
               "fid": flight_id})

        shift_hours = conn.execute(text("""
            SELECT ABS(EXTRACT(EPOCH FROM (:new_dep)::timestamptz - :old_dep) / 3600)
        """), {"new_dep": edit.new_departure_ts,
               "old_dep": flight.departure_ts}).scalar()

        affected = conn.execute(text("""
            SELECT count(*) FROM bookings
            WHERE flight_id = :fid AND status = 'confirmed'
        """), {"fid": flight_id}).scalar()

        if shift_hours > 3 and affected > 0:
            conn.execute(text("""
                UPDATE bookings SET reminder_sent = false
                WHERE flight_id = :fid AND status = 'confirmed'
            """), {"fid": flight_id})

        conn.execute(text("""
            INSERT INTO audit_log (actor, actor_role, action, entity_type, entity_id,
                                   before_state, after_state)
            VALUES ('admin', :role, 'edit_schedule', 'flight', :fid,
                    :before, :after)
        """), {"role": role, "fid": flight_id,
               "before": f'{{"departure_ts": "{flight.departure_ts}", "arrival_ts": "{flight.arrival_ts}"}}',
               "after": f'{{"departure_ts": "{edit.new_departure_ts}", "arrival_ts": "{edit.new_arrival_ts}", "shift_hours": {round(float(shift_hours), 1)}, "affected_bookings": {affected}}}'})

    return {
        "flight_id": flight_id,
        "flight_number": flight.flight_number,
        "shift_hours": round(float(shift_hours), 1),
        "affected_bookings": affected,
        "major_change": bool(shift_hours > 3),
        "message": "Schedule updated" + (
            f" — {affected} booking(s) flagged for re-notification (>3h change)"
            if shift_hours > 3 and affected > 0 else "")
    }


# ---------- Admin: adjust seat class allocation (ops_agent allowed) ----------

@app.patch("/admin/flights/{flight_id}/seat-classes")
def adjust_seat_class(flight_id: int, adj: SeatClassAdjust,
                      x_admin_role: str | None = Header(default=None)):
    role = check_role(x_admin_role, "adjust_seat_class")

    if adj.new_total_seats <= 0:
        raise HTTPException(422, "Seat count must be positive")

    with engine.begin() as conn:
        flight = conn.execute(text("""
            SELECT id, flight_number, status FROM flights WHERE id = :fid
        """), {"fid": flight_id}).first()
        if not flight:
            raise HTTPException(404, f"Flight {flight_id} not found")
        if flight.status == 'cancelled':
            raise HTTPException(409, "Cannot adjust a cancelled flight")

        sc = conn.execute(text("""
            SELECT id, total_seats, available_seats,
                   (total_seats - available_seats) AS booked
            FROM seat_classes
            WHERE flight_id = :fid AND class = :cls
            FOR UPDATE
        """), {"fid": flight_id, "cls": adj.class_name}).first()
        if not sc:
            raise HTTPException(404, f"Class {adj.class_name} not found on this flight")

        if adj.new_total_seats < sc.booked:
            raise HTTPException(422,
                f"Cannot shrink {adj.class_name} to {adj.new_total_seats}: "
                f"{sc.booked} seat(s) already booked")

        new_available = adj.new_total_seats - sc.booked
        conn.execute(text("""
            UPDATE seat_classes
            SET total_seats = :new_total, available_seats = :new_avail
            WHERE id = :scid
        """), {"new_total": adj.new_total_seats,
               "new_avail": new_available, "scid": sc.id})

        conn.execute(text("""
            INSERT INTO audit_log (actor, actor_role, action, entity_type, entity_id,
                                   before_state, after_state)
            VALUES ('admin', :role, 'adjust_seat_class', 'seat_class', :scid,
                    :before, :after)
        """), {"role": role, "scid": sc.id,
               "before": f'{{"class": "{adj.class_name}", "total": {sc.total_seats}, "available": {sc.available_seats}}}',
               "after": f'{{"class": "{adj.class_name}", "total": {adj.new_total_seats}, "available": {new_available}, "booked": {sc.booked}}}'})

    return {"flight_id": flight_id, "class": adj.class_name,
            "total_seats": adj.new_total_seats,
            "available_seats": new_available,
            "booked_seats": sc.booked}
    
    import uuid
from datetime import datetime, timezone

# ---------- Booking: create with hold ----------

HOLD_MINUTES = 15   # price-hold duration between search and payment (file requirement)

class BookingCreate(BaseModel):
    flight_id: int
    passenger_id: int
    seat_class: str            # first / business / economy
    fare: str                  # basic_economy / flexible (your enum's labels)
    idempotency_key: str       # client-generated unique key per booking attempt


@app.post("/bookings", status_code=201)
def create_booking(req: BookingCreate):
    with engine.begin() as conn:
        # 1. IDEMPOTENCY: same key = return the same booking, never book twice (file requirement)
        existing = conn.execute(text("""
            SELECT id, status FROM bookings WHERE idempotency_key = :key
        """), {"key": req.idempotency_key}).first()
        if existing:
            return {"booking_id": existing.id, "status": str(existing.status),
                    "idempotent_replay": True,
                    "message": "This booking request was already processed"}

        # 2. Flight must be bookable
        flight = conn.execute(text("""
            SELECT id, flight_number, status, departure_ts FROM flights WHERE id = :fid
        """), {"fid": req.flight_id}).first()
        if not flight:
            raise HTTPException(404, "Flight not found")
        if flight.status != 'scheduled':
            raise HTTPException(409, f"Flight is {flight.status}, not bookable")

        # 3. THE ATOMIC DECREMENT — check-and-take in one statement.
        #    Two racing requests on the last seat: exactly one wins.
        seat = conn.execute(text("""
            UPDATE seat_classes
            SET available_seats = available_seats - 1
            WHERE flight_id = :fid AND class = :cls AND available_seats >= 1
            RETURNING id, base_price
        """), {"fid": req.flight_id, "cls": req.seat_class}).first()

        if not seat:
            raise HTTPException(409,
                f"No {req.seat_class} seats available on this flight")

        # 4. Create the booking as a HOLD with expiry (file requirement)
        booking_id = conn.execute(text("""
            INSERT INTO bookings (idempotency_key, flight_id, passenger_id,
                                  seat_class, fare, price_paid, currency,
                                  status, hold_expires_at)
            VALUES (:key, :fid, :pid, :cls, :fare, :price, 'USD',
                    'held', now() + interval '15 minutes')
            RETURNING id
        """), {"key": req.idempotency_key, "fid": req.flight_id,
               "pid": req.passenger_id, "cls": req.seat_class,
               "fare": req.fare, "price": seat.base_price}).scalar()

    return {"booking_id": booking_id, "status": "held",
            "price": float(seat.base_price),
            "hold_expires_in_minutes": HOLD_MINUTES,
            "message": "Seat held. Confirm payment before expiry or the seat is released."}


# ---------- Booking: confirm (payment succeeded) ----------

@app.post("/bookings/{booking_id}/confirm")
def confirm_booking(booking_id: int):
    with engine.begin() as conn:
        # Only a live, unexpired hold can confirm — atomic again
        row = conn.execute(text("""
            UPDATE bookings
            SET status = 'confirmed', updated_at = now()
            WHERE id = :bid AND status = 'held' AND hold_expires_at > now()
            RETURNING id, flight_id, seat_class, price_paid
        """), {"bid": booking_id}).first()

        if not row:
            # Diagnose why for a useful error
            b = conn.execute(text("""
                SELECT status, hold_expires_at FROM bookings WHERE id = :bid
            """), {"bid": booking_id}).first()
            if not b:
                raise HTTPException(404, "Booking not found")
            if b.status == 'confirmed':
                raise HTTPException(409, "Booking already confirmed")
            raise HTTPException(409,
                f"Hold expired or booking is '{b.status}' — seat may have been released")

        # Record the payment
        conn.execute(text("""
            INSERT INTO payments (booking_id, amount, status, paid_at)
            VALUES (:bid, :amt, 'completed', now())
        """), {"bid": booking_id, "amt": row.price_paid})

    return {"booking_id": booking_id, "status": "confirmed",
            "amount_paid": float(row.price_paid)}
    
    
    # ---------- Booking: group (all-or-nothing) ----------

class GroupBookingCreate(BaseModel):
    flight_id: int
    passenger_ids: list[int]      # one seat per passenger
    seat_class: str
    fare: str
    idempotency_key: str


@app.post("/bookings/group", status_code=201)
def create_group_booking(req: GroupBookingCreate):
    n = len(req.passenger_ids)
    if n < 1:
        raise HTTPException(422, "At least one passenger required")

    with engine.begin() as conn:
        # Idempotency for the whole group
        existing = conn.execute(text("""
            SELECT group_id FROM bookings WHERE idempotency_key = :key
        """), {"key": req.idempotency_key + "-p0"}).first()
        if existing:
            return {"group_id": str(existing.group_id),
                    "idempotent_replay": True}

        flight = conn.execute(text("""
            SELECT id, status FROM flights WHERE id = :fid
        """), {"fid": req.flight_id}).first()
        if not flight:
            raise HTTPException(404, "Flight not found")
        if flight.status != 'scheduled':
            raise HTTPException(409, f"Flight is {flight.status}")

        # THE GROUP ATOMIC DECREMENT: take all N or none
        seat = conn.execute(text("""
            UPDATE seat_classes
            SET available_seats = available_seats - :n
            WHERE flight_id = :fid AND class = :cls AND available_seats >= :n
            RETURNING base_price
        """), {"n": n, "fid": req.flight_id, "cls": req.seat_class}).first()

        if not seat:
            avail = conn.execute(text("""
                SELECT available_seats FROM seat_classes
                WHERE flight_id = :fid AND class = :cls
            """), {"fid": req.flight_id, "cls": req.seat_class}).scalar()
            raise HTTPException(409,
                f"Need {n} {req.seat_class} seats, only {avail} available — full fail (no partial holds)")

        # One booking per passenger, tied by group_id
        group_id = str(uuid.uuid4())
        booking_ids = []
        for i, pid in enumerate(req.passenger_ids):
            bid = conn.execute(text("""
                INSERT INTO bookings (idempotency_key, flight_id, passenger_id,
                                      seat_class, fare, price_paid, currency,
                                      status, hold_expires_at, group_id)
                VALUES (:key, :fid, :pid, :cls, :fare, :price, 'USD',
                        'held', now() + interval '15 minutes', :gid)
                RETURNING id
            """), {"key": f"{req.idempotency_key}-p{i}", "fid": req.flight_id,
                   "pid": pid, "cls": req.seat_class, "fare": req.fare,
                   "price": seat.base_price, "gid": group_id}).scalar()
            booking_ids.append(bid)

    return {"group_id": group_id, "booking_ids": booking_ids,
            "seats_held": n, "price_each": float(seat.base_price),
            "hold_expires_in_minutes": HOLD_MINUTES}
    
    
    # ---------- Booking: cancel (fare-type branching) ----------

@app.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: int):
    with engine.begin() as conn:
        # Lock the booking row — no concurrent cancel/confirm race
        b = conn.execute(text("""
            SELECT b.id, b.status, b.fare, b.seat_class, b.price_paid,
                   b.flight_id, f.departure_ts, f.status AS flight_status
            FROM bookings b
            JOIN flights f ON f.id = b.flight_id
            WHERE b.id = :bid
            FOR UPDATE OF b
        """), {"bid": booking_id}).first()

        if not b:
            raise HTTPException(404, "Booking not found")
        if b.status == 'cancelled':
            raise HTTPException(409, "Booking already cancelled")
        if b.status not in ('held', 'confirmed'):
            raise HTTPException(409, f"Cannot cancel a booking in status '{b.status}'")

        # Hours until departure — drives the refund decision
        hours_left = conn.execute(text("""
            SELECT EXTRACT(EPOCH FROM (:dep)::timestamptz - now()) / 3600
        """), {"dep": b.departure_ts}).scalar()

        # FARE-TYPE BRANCHING (mirrors the Pinecone policy doc the RAG agent quotes)
        fare = str(b.fare)
        was_paid = b.status == 'confirmed'

        if not was_paid:
            # A held (unpaid) booking: cancel freely, nothing to refund
            refund_amount, refund_reason = 0, "no payment taken (hold released)"
        elif fare == 'flexible':
            if hours_left > 24:
                refund_amount, refund_reason = float(b.price_paid), "flexible fare, >24h before departure: full refund"
            else:
                refund_amount, refund_reason = 0, "flexible fare, <24h before departure: no refund"
        elif fare == 'basic_economy':
            refund_amount, refund_reason = 0, "basic economy: non-refundable"
        else:
            # Unknown fare label — refuse rather than guess with money
            raise HTTPException(422, f"No cancellation rule for fare '{fare}'")

        # Cancel the booking
        conn.execute(text("""
            UPDATE bookings SET status = 'cancelled', updated_at = now()
            WHERE id = :bid
        """), {"bid": booking_id})

        # Release the seat back to inventory (atomic, as always)
        conn.execute(text("""
            UPDATE seat_classes SET available_seats = available_seats + 1
            WHERE flight_id = :fid AND class = :cls
        """), {"fid": b.flight_id, "cls": b.seat_class})

        # Create the pending refund if money is owed
        refund_id = None
        if refund_amount > 0:
            refund_id = conn.execute(text("""
                INSERT INTO refunds (booking_id, amount, status, created_at)
                VALUES (:bid, :amt, 'pending', now())
                RETURNING id
            """), {"bid": booking_id, "amt": refund_amount}).scalar()

        # Audit
        conn.execute(text("""
            INSERT INTO audit_log (actor, actor_role, action, entity_type, entity_id,
                                   before_state, after_state)
            VALUES ('customer', 'ops_agent', 'cancel_booking', 'booking', :bid,
                    :before, :after)
        """), {"bid": booking_id,
               "before": f'{{"status": "{b.status}", "fare": "{fare}"}}',
               "after": f'{{"status": "cancelled", "refund": {refund_amount}, "reason": "{refund_reason}"}}'})

    return {"booking_id": booking_id, "status": "cancelled",
            "fare": fare, "hours_before_departure": round(float(hours_left), 1),
            "refund_amount": refund_amount, "refund_id": refund_id,
            "reason": refund_reason,
            "seat_released": True}
    
    # ---------- Public: search available seats ----------

@app.get("/search")
def search_flights(origin: str, destination: str, date: str):
    """Available seats per class for a route/date. date format: YYYY-MM-DD"""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT f.id AS flight_id, f.flight_number,
                   f.origin, f.destination,
                   f.departure_ts, f.arrival_ts,
                   sc.class, sc.available_seats, sc.base_price, sc.currency,
                   sc.booking_cutoff_minutes
            FROM flights f
            JOIN seat_classes sc ON sc.flight_id = f.id
            WHERE f.origin = :orig
              AND f.destination = :dest
              AND f.departure_ts::date = (:d)::date
              AND f.status = 'scheduled'
              AND f.departure_ts > now()
              AND sc.available_seats > 0
            ORDER BY f.departure_ts, sc.base_price
        """), {"orig": origin, "dest": destination, "d": date}).fetchall()

    if not rows:
        return {"flights": [], "message": "No available flights for this route/date"}

    # Group by flight
    flights = {}
    for r in rows:
        fid = r.flight_id
        if fid not in flights:
            flights[fid] = {
                "flight_id": fid, "flight_number": r.flight_number,
                "origin": r.origin, "destination": r.destination,
                "departure_ts": str(r.departure_ts), "arrival_ts": str(r.arrival_ts),
                "classes": []
            }
        flights[fid]["classes"].append({
            "class": str(r.class_) if hasattr(r, 'class_') else str(r[6]),
            "available_seats": r.available_seats,
            "price": float(r.base_price),
            "currency": str(r.currency).strip(),
        })

    return {"flights": list(flights.values()), "count": len(flights)}


# ---------- Booking: join waitlist when class is full ----------

class WaitlistJoin(BaseModel):
    flight_id: int
    passenger_id: int
    seat_class: str


@app.post("/waitlist", status_code=201)
def join_waitlist(req: WaitlistJoin):
    with engine.begin() as conn:
        flight = conn.execute(text("""
            SELECT id, status FROM flights WHERE id = :fid
        """), {"fid": req.flight_id}).first()
        if not flight:
            raise HTTPException(404, "Flight not found")
        if flight.status != 'scheduled':
            raise HTTPException(409, f"Flight is {flight.status}")

        # Rule: waitlist only when the class is FULL
        avail = conn.execute(text("""
            SELECT available_seats FROM seat_classes
            WHERE flight_id = :fid AND class = :cls
        """), {"fid": req.flight_id, "cls": req.seat_class}).scalar()
        if avail is None:
            raise HTTPException(404, f"Class {req.seat_class} not on this flight")
        if avail > 0:
            raise HTTPException(409,
                f"{avail} {req.seat_class} seat(s) still available — book directly instead of waitlisting")

        # Duplicate guard: same passenger, same flight/class, still waiting
        dup = conn.execute(text("""
            SELECT id FROM waitlist
            WHERE flight_id = :fid AND passenger_id = :pid
              AND seat_class = :cls AND status = 'waiting'
        """), {"fid": req.flight_id, "pid": req.passenger_id,
               "cls": req.seat_class}).first()
        if dup:
            raise HTTPException(409, "Already on this waitlist")

        wl_id = conn.execute(text("""
            INSERT INTO waitlist (flight_id, passenger_id, seat_class, status, joined_at)
            VALUES (:fid, :pid, :cls, 'waiting', now())
            RETURNING id
        """), {"fid": req.flight_id, "pid": req.passenger_id,
               "cls": req.seat_class}).scalar()

        # Position in queue (priority rule: joined_at — first come, first served)
        position = conn.execute(text("""
            SELECT count(*) FROM waitlist
            WHERE flight_id = :fid AND seat_class = :cls
              AND status = 'waiting'
              AND joined_at <= (SELECT joined_at FROM waitlist WHERE id = :wid)
        """), {"fid": req.flight_id, "cls": req.seat_class, "wid": wl_id}).scalar()

    return {"waitlist_id": wl_id, "position": position,
            "message": f"Added to {req.seat_class} waitlist at position {position}"}