import os
import uuid
import json
import smtplib
import logging
from email.message import EmailMessage
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
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = logging.getLogger("flight_management")


# ---------- Transactional email (request-triggered — FastAPI owns this,
# distinct from n8n's scheduled/background Gmail sends) ----------

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")


def send_transactional_email(to_addr: str | None, subject: str, body: str) -> bool:
    """Best-effort transactional send via Gmail SMTP. Never raises — a booking
    or cancellation must still succeed even if the email fails to send."""
    if not to_addr:
        logger.warning("No recipient email on file; skipping send: %s", subject)
        return False
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        logger.warning("GMAIL_ADDRESS/GMAIL_APP_PASSWORD not configured; skipping send: %s", subject)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = to_addr
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as exc:
        logger.error("Failed to send transactional email (%s): %s", subject, exc)
        return False


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


# ---------- Admin: create flight (super_admin only, idempotent via header) ----------

@app.post("/admin/flights", status_code=201)
def create_flight(flight: FlightCreate,
                  x_admin_role: str | None = Header(default=None),
                  idempotency_key: str = Header(..., alias="Idempotency-Key")):
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
        # IDEMPOTENCY: same header key on a retried create = return the same flight
        if idempotency_key:
            existing = conn.execute(text("""
                SELECT entity_id FROM audit_log
                WHERE action = 'create_flight'
                  AND after_state->>'idempotency_key' = :key
            """), {"key": idempotency_key}).first()
            if existing:
                return {"flight_id": existing.entity_id,
                        "idempotent_replay": True,
                        "message": "This create-flight request was already processed"}

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

            # Generate the physical seat map for this class
            prefix = sc.class_name[0].upper()   # 'F', 'B', 'E'
            for seat_num in range(1, sc.total_seats + 1):
                conn.execute(text("""
                    INSERT INTO seats (flight_id, seat_number, class, is_occupied)
                    VALUES (:fid, :sn, :cls, false)
                """), {"fid": flight_id, "sn": f"{prefix}{seat_num}", "cls": sc.class_name})

        conn.execute(text("""
            INSERT INTO audit_log (actor, actor_role, action, entity_type, entity_id, after_state)
            VALUES ('admin', :role, 'create_flight', 'flight', :fid, :after)
        """), {"role": role, "fid": flight_id,
               "after": f'{{"flight_number": "{flight.flight_number}", "capacity": {flight.total_capacity}, "status": "scheduled", "idempotency_key": {f"{chr(34)}{idempotency_key}{chr(34)}" if idempotency_key else "null"}}}'})

    return {"flight_id": flight_id, "message": f"Flight {flight.flight_number} created"}

class FlightCancelOptions(BaseModel):
    resolution: str = "refund"   # "refund" or "credit"
    credit_validity_days: int = 180

# ---------- Admin: cancel flight (super_admin only, idempotent via header) ----------

@app.post("/admin/flights/{flight_id}/cancel")
def cancel_flight(flight_id: int,
                  opts: FlightCancelOptions = FlightCancelOptions(),
                  x_admin_role: str | None = Header(default=None),
                  idempotency_key: str = Header(..., alias="Idempotency-Key")):
    role = check_role(x_admin_role, "cancel_flight")

    if opts.resolution not in ("refund", "credit"):
        raise HTTPException(422, "resolution must be 'refund' or 'credit'")

    with engine.begin() as conn:
        if idempotency_key:
            existing = conn.execute(text("""
                SELECT entity_id, after_state FROM audit_log
                WHERE action = 'cancel_flight'
                  AND after_state->>'idempotency_key' = :key
            """), {"key": idempotency_key}).first()
            if existing:
                return {"flight_id": existing.entity_id,
                        "idempotent_replay": True,
                        "message": "This cancel-flight request was already processed"}

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

        confirmed_bookings = conn.execute(text("""
            SELECT id, price_paid FROM bookings
            WHERE flight_id = :fid AND status = 'confirmed'
        """), {"fid": flight_id}).fetchall()

        refunds_created = []
        for b in confirmed_bookings:
            if opts.resolution == "credit":
                rid = conn.execute(text("""
                    INSERT INTO refunds (booking_id, amount, status, is_credit,
                                        credit_expires_at, created_at)
                    VALUES (:bid, :amt,'approved' , true,
                            now() + (:days || ' days')::interval, now())
                    RETURNING id
                """), {"bid": b.id, "amt": b.price_paid,
                       "days": opts.credit_validity_days}).scalar()
            else:
                rid = conn.execute(text("""
                    INSERT INTO refunds (booking_id, amount, status, is_credit, created_at)
                    VALUES (:bid, :amt, 'pending', false, now())
                    RETURNING id
                """), {"bid": b.id, "amt": b.price_paid}).scalar()
            refunds_created.append(rid)

        key_json = f'"{idempotency_key}"' if idempotency_key else "null"
        conn.execute(text("""
            INSERT INTO audit_log (actor, actor_role, action, entity_type, entity_id,
                                   before_state, after_state)
            VALUES ('admin', :role, 'cancel_flight', 'flight', :fid,
                    :before, :after)
        """), {"role": role, "fid": flight_id,
               "before": f'{{"status": "{before_status}"}}',
               "after": f'{{"status": "cancelled", "resolution": "{opts.resolution}", "refunds_created": {len(refunds_created)}, "idempotency_key": {key_json}}}'})

    return {"flight_id": flight_id,
            "flight_number": flight.flight_number,
            "status": "cancelled",
            "resolution": opts.resolution,
            "refunds_created": len(refunds_created),
            "message": (
                f"{len(refunds_created)} travel credit(s) issued, valid {opts.credit_validity_days} days"
                if opts.resolution == "credit"
                else f"{len(refunds_created)} cash refund(s) created as pending"
            )}

# ---------- Admin: edit flight schedule (ops_agent allowed) ----------

@app.patch("/admin/flights/{flight_id}/schedule")
def edit_schedule(flight_id: int, edit: ScheduleEdit,
                  x_admin_role: str | None = Header(default=None),
                  idempotency_key: str = Header(..., alias="Idempotency-Key")):
    role = check_role(x_admin_role, "edit_schedule")

    with engine.begin() as conn:
        # IDEMPOTENCY: same key = do not process the schedule change twice
        existing = conn.execute(text("""
            SELECT entity_id, after_state
            FROM audit_log
            WHERE action = 'edit_schedule'
              AND after_state->>'idempotency_key' = :key
        """), {"key": idempotency_key}).first()

        if existing:
            return {
                "flight_id": existing.entity_id,
                "idempotent_replay": True,
                "message": "This schedule-edit request was already processed"
            }

        # Lock the flight so another admin action cannot change it at the same time.
        flight = conn.execute(text("""
            SELECT id, flight_number, origin, destination, status,
                   departure_ts, arrival_ts
            FROM flights
            WHERE id = :fid
            FOR UPDATE
        """), {"fid": flight_id}).first()

        if not flight:
            raise HTTPException(404, f"Flight {flight_id} not found")
        if flight.status == 'cancelled':
            raise HTTPException(409, "Cannot reschedule a cancelled flight")

        # Validate the new schedule before changing the database.
        if edit.new_arrival_ts <= edit.new_departure_ts:
            raise HTTPException(422, "Arrival time must be after departure time")

        # Calculate the departure shift from the ORIGINAL schedule.
        shift_hours = conn.execute(text("""
            SELECT ABS(EXTRACT(EPOCH FROM (
                (:new_dep)::timestamptz - (:old_dep)::timestamptz
            )) / 3600)
        """), {
            "new_dep": edit.new_departure_ts,
            "old_dep": flight.departure_ts
        }).scalar()
        shift_hours = float(shift_hours or 0)

        # Get all confirmed bookings before changing flight_id values.
        bookings = conn.execute(text("""
            SELECT id, passenger_id, seat_class, fare, price_paid
            FROM bookings
            WHERE flight_id = :fid
              AND status = 'confirmed'
            ORDER BY id
            FOR UPDATE
        """), {"fid": flight_id}).mappings().all()

        # Apply the new schedule first. If any automatic rebooking operation
        # fails, engine.begin() rolls the entire transaction back.
        conn.execute(text("""
            UPDATE flights
            SET departure_ts = :dep,
                arrival_ts = :arr,
                updated_at = now()
            WHERE id = :fid
        """), {
            "dep": edit.new_departure_ts,
            "arr": edit.new_arrival_ts,
            "fid": flight_id
        })

        rebooked = []
        rebooking_failures = []

        # Requirement: a major (>3h) schedule change should trigger an
        # automatic rebooking attempt for confirmed passengers.
        if shift_hours > 3 and bookings:
            # Find the closest scheduled flight on the SAME route that can hold
            # all affected passengers in their existing seat classes.
            class_counts = {}
            for booking in bookings:
                cls = str(booking["seat_class"])
                class_counts[cls] = class_counts.get(cls, 0) + 1

            candidates = conn.execute(text("""
                SELECT id, flight_number, departure_ts, arrival_ts
                FROM flights
                WHERE id <> :fid
                  AND origin = :origin
                  AND destination = :destination
                  AND status = 'scheduled'
                  AND departure_ts >= (:new_dep)::timestamptz
                ORDER BY ABS(EXTRACT(EPOCH FROM (
                    departure_ts - (:new_dep)::timestamptz
                )))
            """), {
                "fid": flight_id,
                "origin": flight.origin,
                "destination": flight.destination,
                "new_dep": edit.new_departure_ts
            }).fetchall()

            chosen = None

            for candidate in candidates:
                # Lock candidate seat rows before checking and reserving them.
                candidate_classes = conn.execute(text("""
                    SELECT id, class, available_seats
                    FROM seat_classes
                    WHERE flight_id = :fid
                    FOR UPDATE
                """), {"fid": candidate.id}).mappings().all()

                available_by_class = {
                    str(row["class"]): row["available_seats"]
                    for row in candidate_classes
                }

                if all(
                    available_by_class.get(cls, 0) >= count
                    for cls, count in class_counts.items()
                ):
                    chosen = (candidate, candidate_classes)
                    break

            if chosen:
                candidate, candidate_classes = chosen
                candidate_class_ids = {
                    str(row["class"]): row["id"]
                    for row in candidate_classes
                }

                # Reserve the required inventory on the alternate flight.
                for cls, count in class_counts.items():
                    updated = conn.execute(text("""
                        UPDATE seat_classes
                        SET available_seats = available_seats - :count
                        WHERE id = :scid
                          AND available_seats >= :count
                    """), {
                        "count": count,
                        "scid": candidate_class_ids[cls]
                    }).rowcount

                    if updated != 1:
                        raise HTTPException(
                            409,
                            "Automatic rebooking failed because alternate-flight "
                            "inventory changed during the transaction"
                        )

                    # Return the passengers' inventory to the changed flight.
                    conn.execute(text("""
                        UPDATE seat_classes
                        SET available_seats = available_seats + :count
                        WHERE flight_id = :fid AND class = :cls
                    """), {
                        "count": count,
                        "fid": flight_id,
                        "cls": cls
                    })

                # Move each confirmed booking to the alternate flight.
                for booking in bookings:
                    before_state = {
                        "booking_id": booking["id"],
                        "flight_id": flight_id,
                        "seat_class": str(booking["seat_class"])
                    }
                    after_state = {
                        "booking_id": booking["id"],
                        "flight_id": candidate.id,
                        "flight_number": candidate.flight_number,
                        "seat_class": str(booking["seat_class"]),
                        "reason": "major_schedule_change",
                        "original_schedule_shift_hours": round(shift_hours, 1)
                    }

                    conn.execute(text("""
                        UPDATE bookings
                        SET flight_id = :new_fid,
                            reminder_sent = false,
                            updated_at = now()
                        WHERE id = :bid
                          AND status = 'confirmed'
                    """), {
                        "new_fid": candidate.id,
                        "bid": booking["id"]
                    })

                    # admin_role is an enum containing super_admin/ops_agent,
                    # so the system action uses the current authorized admin role.
                    conn.execute(text("""
                        INSERT INTO audit_log (
                            actor, actor_role, action, entity_type, entity_id,
                            before_state, after_state
                        )
                        VALUES (
                            'system', :role, 'automatic_rebooking', 'booking', :booking_id,
                            CAST(:before_state AS jsonb),
                            CAST(:after_state AS jsonb)
                        )
                    """), {
                        "role": role,
                        "booking_id": booking["id"],
                        "before_state": json.dumps(before_state),
                        "after_state": json.dumps(after_state)
                    })

                    rebooked.append({
                        "booking_id": booking["id"],
                        "passenger_id": booking["passenger_id"],
                        "old_flight_id": flight_id,
                        "new_flight_id": candidate.id,
                        "new_flight_number": candidate.flight_number,
                        "seat_class": str(booking["seat_class"])
                    })
            else:
                # No suitable same-route flight was found. Keep the booking on
                # the changed flight and flag it for notification/escalation.
                for booking in bookings:
                    conn.execute(text("""
                        UPDATE bookings
                        SET reminder_sent = false,
                            updated_at = now()
                        WHERE id = :bid
                    """), {"bid": booking["id"]})

                rebooking_failures = [
                    {
                        "booking_id": booking["id"],
                        "reason": "No same-route alternate flight with enough seat-class capacity"
                    }
                    for booking in bookings
                ]

        # Normal (<3h) changes do not require automatic rebooking.
        elif shift_hours > 3:
            rebooking_failures = []

        audit_after = {
            "departure_ts": edit.new_departure_ts,
            "arrival_ts": edit.new_arrival_ts,
            "shift_hours": round(shift_hours, 1),
            "affected_bookings": len(bookings),
            "rebooked_booking_ids": [item["booking_id"] for item in rebooked],
            "rebooking_failures": rebooking_failures,
            "idempotency_key": idempotency_key
        }

        conn.execute(text("""
            INSERT INTO audit_log (
                actor, actor_role, action, entity_type, entity_id,
                before_state, after_state
            )
            VALUES (
                'admin', :role, 'edit_schedule', 'flight', :fid,
                CAST(:before AS jsonb), CAST(:after AS jsonb)
            )
        """), {
            "role": role,
            "fid": flight_id,
            "before": json.dumps({
                "departure_ts": str(flight.departure_ts),
                "arrival_ts": str(flight.arrival_ts)
            }),
            "after": json.dumps(audit_after)
        })

    message = "Schedule updated"
    if shift_hours > 3 and rebooked:
        message += (
            f" — {len(rebooked)} booking(s) automatically rebooked "
            f"to {rebooked[0]['new_flight_number']}"
        )
    elif shift_hours > 3 and rebooking_failures:
        message += (
            f" — {len(rebooking_failures)} booking(s) could not be automatically "
            "rebooked and were flagged for follow-up"
        )

    return {
        "flight_id": flight_id,
        "flight_number": flight.flight_number,
        "shift_hours": round(shift_hours, 1),
        "affected_bookings": len(bookings),
        "major_change": bool(shift_hours > 3),
        "automatic_rebooking": {
            "rebooked_count": len(rebooked),
            "rebooked": rebooked,
            "failed_count": len(rebooking_failures),
            "failures": rebooking_failures
        },
        "message": message
    }

# ---------- Admin: adjust seat class allocation (ops_agent allowed) ----------

@app.patch("/admin/flights/{flight_id}/seat-classes")
def adjust_seat_class(flight_id: int, adj: SeatClassAdjust,
                      x_admin_role: str | None = Header(default=None),
                      idempotency_key: str = Header(..., alias="Idempotency-Key")):
    role = check_role(x_admin_role, "adjust_seat_class")

    if adj.new_total_seats <= 0:
        raise HTTPException(422, "Seat count must be positive")

    with engine.begin() as conn:
        # IDEMPOTENCY: same key = do not adjust the same class twice
        existing = conn.execute(text("""
            SELECT entity_id, after_state
            FROM audit_log
            WHERE action = 'adjust_seat_class'
              AND after_state->>'idempotency_key' = :key
        """), {"key": idempotency_key}).first()

        if existing:
            return {
                "seat_class_id": existing.entity_id,
                "flight_id": existing.after_state.get("flight_id"),
                "class": existing.after_state.get("class"),
                "total_seats": existing.after_state.get("total"),
                "available_seats": existing.after_state.get("available"),
                "booked_seats": existing.after_state.get("booked"),
                "idempotent_replay": True,
                "message": "This seat-class adjustment was already processed"
            }

        flight = conn.execute(text("""
            SELECT id, flight_number, status FROM flights WHERE id = :fid
            FOR UPDATE
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
               "after": f'{{"flight_id": {flight_id}, "class": "{adj.class_name}", "total": {adj.new_total_seats}, "available": {new_available}, "booked": {sc.booked}, "idempotency_key": "{idempotency_key}"}}'})

    return {"flight_id": flight_id, "class": adj.class_name,
            "total_seats": adj.new_total_seats,
            "available_seats": new_available,
            "booked_seats": sc.booked}


# ---------- Booking: create with hold (idempotent via header) ----------

HOLD_MINUTES = 15   # price-hold duration between search and payment (file requirement)

class BookingCreate(BaseModel):
    flight_id: int
    passenger_id: int
    seat_class: str            # first / business / economy
    fare: str                  # basic_economy / flexible (your enum's labels)
    requested_seat_number: str | None = None   # optional specific seat choice

class ItineraryLeg(BaseModel):
    flight_id: int
    seat_class: str


class ItineraryCreate(BaseModel):
    passenger_id: int
    fare: str
    legs: list[ItineraryLeg] = Field(min_length=2)

@app.post("/bookings", status_code=201)
def create_booking(req: BookingCreate,
                   idempotency_key: str = Header(..., alias="Idempotency-Key")):
    with engine.begin() as conn:
        # 1. IDEMPOTENCY: same header key = return the same booking, never book twice
        existing = conn.execute(text("""
            SELECT id, status FROM bookings WHERE idempotency_key = :key
        """), {"key": idempotency_key}).first()
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
            RETURNING id, base_price, booking_cutoff_minutes, currency
        """), {"fid": req.flight_id, "cls": req.seat_class}).first()

        if not seat:
            raise HTTPException(409,
                f"No {req.seat_class} seats available on this flight")
                # FARE RESTRICTION: basic economy gets no seat choice (file requirement)
        if req.requested_seat_number is not None and req.fare == 'basic_economy':
            raise HTTPException(422,
                "Basic economy fare does not allow seat selection; "
                "a seat will be auto-assigned")

        # If a specific seat was requested (flexible fare only), mark it occupied
        assigned_seat_id = None
        if req.requested_seat_number is not None:
            seat_row = conn.execute(text("""
                UPDATE seats
                SET is_occupied = true
                WHERE flight_id = :fid AND seat_number = :sn
                  AND class = :cls AND is_occupied = false
                RETURNING id
            """), {"fid": req.flight_id, "sn": req.requested_seat_number,
                   "cls": req.seat_class}).first()
            if not seat_row:
                raise HTTPException(409,
                    f"Seat {req.requested_seat_number} is unavailable or does not exist")
            assigned_seat_id = seat_row.id

        # CUTOFF CHECK: class-specific booking cutoff (e.g. First/Business allow later cutoff)
        minutes_left = conn.execute(text("""
            SELECT EXTRACT(EPOCH FROM (:dep)::timestamptz - now()) / 60
        """), {"dep": flight.departure_ts}).scalar()

        if seat.booking_cutoff_minutes is not None and minutes_left < seat.booking_cutoff_minutes:
            raise HTTPException(422,
                f"Booking closed for {req.seat_class}: cutoff is "
                f"{seat.booking_cutoff_minutes} min before departure, "
                f"only {round(float(minutes_left))} min remain")

        # 4. Create the booking as a HOLD with expiry (file requirement)
        booking_id = conn.execute(text("""
            INSERT INTO bookings (idempotency_key, flight_id, passenger_id,
                                  seat_class, seat_id, fare, price_paid, currency,
                                  status, hold_expires_at)
            VALUES (:key, :fid, :pid, :cls, :sid, :fare, :price, :cur,
                    'held', now() + interval '15 minutes')
            RETURNING id
        """), {"key": idempotency_key, "fid": req.flight_id,
               "pid": req.passenger_id, "cls": req.seat_class,
               "sid": assigned_seat_id,
               "fare": req.fare, "price": seat.base_price,
               "cur": str(seat.currency).strip()}).scalar()

    return {"booking_id": booking_id, "status": "held",
            "price": float(seat.base_price),
            "hold_expires_in_minutes": HOLD_MINUTES,
            "message": "Seat held. Confirm payment before expiry or the seat is released."}


# ---------- Connecting itinerary: atomic multi-leg hold ----------

@app.post("/bookings/itinerary", status_code=201)
def create_itinerary(
    req: ItineraryCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key")
):
    """
    Create a multi-leg itinerary atomically.

    All legs must be available.
    If any leg fails, the entire transaction rolls back,
    so no partial itinerary is created.
    """

    if len(req.legs) < 2:
        raise HTTPException(
            422,
            "A connecting itinerary must contain at least 2 legs"
        )

    # Prevent the same flight from being used twice
    flight_ids = [leg.flight_id for leg in req.legs]

    if len(flight_ids) != len(set(flight_ids)):
        raise HTTPException(
            422,
            "The same flight cannot appear more than once in an itinerary"
        )

    with engine.begin() as conn:

        # ---------------------------------------------------------
        # 1. IDEMPOTENCY
        # ---------------------------------------------------------
        existing = conn.execute(text("""
            SELECT id, group_id
            FROM bookings
            WHERE idempotency_key = :key
               OR idempotency_key LIKE :prefix
            ORDER BY id
        """), {
            "key": idempotency_key,
            "prefix": f"{idempotency_key}-leg-%"
        }).fetchall()

        if existing:
            return {
                "idempotent_replay": True,
                "booking_ids": [row.id for row in existing],
                "message": "This itinerary request was already processed"
            }

        # ---------------------------------------------------------
        # 2. LOCK ALL FLIGHTS IN A CONSISTENT ORDER
        # ---------------------------------------------------------
        locked_flights = {}

        for flight_id in sorted(flight_ids):
            flight = conn.execute(text("""
                SELECT
                    id,
                    flight_number,
                    origin,
                    destination,
                    departure_ts,
                    arrival_ts,
                    status
                FROM flights
                WHERE id = :fid
                FOR UPDATE
            """), {
                "fid": flight_id
            }).first()

            if not flight:
                raise HTTPException(
                    404,
                    f"Flight {flight_id} not found"
                )

            if flight.status != "scheduled":
                raise HTTPException(
                    409,
                    f"Flight {flight_id} is {flight.status}, not bookable"
                )

            locked_flights[flight_id] = flight

        # ---------------------------------------------------------
        # 3. VALIDATE CONNECTION
        # ---------------------------------------------------------
        for i in range(len(req.legs) - 1):

            current = locked_flights[req.legs[i].flight_id]
            next_flight = locked_flights[req.legs[i + 1].flight_id]

            if current.destination != next_flight.origin:
                raise HTTPException(
                    422,
                    f"Invalid connection: "
                    f"{current.destination} does not connect to "
                    f"{next_flight.origin}"
                )

            if current.arrival_ts >= next_flight.departure_ts:
                raise HTTPException(
                    422,
                    f"Invalid connection timing between "
                    f"{current.flight_number} and "
                    f"{next_flight.flight_number}"
                )

        # ---------------------------------------------------------
        # 4. CREATE COMMON ITINERARY/GROUP ID
        # ---------------------------------------------------------
        itinerary_id = str(uuid.uuid4())

        booking_ids = []
        total_price = 0

        # ---------------------------------------------------------
        # 5. ATOMICALLY HOLD EVERY LEG
        # ---------------------------------------------------------
        for index, leg in enumerate(req.legs):

            flight = locked_flights[leg.flight_id]

            # Check and decrement this leg's inventory.
            # If ANY leg fails, the whole transaction rolls back.
            seat = conn.execute(text("""
                UPDATE seat_classes
                SET available_seats = available_seats - 1
                WHERE flight_id = :fid
                  AND class = :cls
                  AND available_seats >= 1
                RETURNING base_price, available_seats
            """), {
                "fid": leg.flight_id,
                "cls": leg.seat_class
            }).first()

            if not seat:
                raise HTTPException(
                    409,
                    f"No {leg.seat_class} seats available on "
                    f"flight {flight.flight_number}. "
                    f"Entire itinerary was NOT booked."
                )

            # Class-specific cutoff
            minutes_left = conn.execute(text("""
                SELECT EXTRACT(
                    EPOCH FROM (:dep)::timestamptz - now()
                ) / 60
            """), {
                "dep": flight.departure_ts
            }).scalar()

            cutoff = conn.execute(text("""
                SELECT booking_cutoff_minutes
                FROM seat_classes
                WHERE flight_id = :fid
                  AND class = :cls
            """), {
                "fid": leg.flight_id,
                "cls": leg.seat_class
            }).scalar()

            if cutoff is not None and minutes_left < cutoff:
                raise HTTPException(
                    422,
                    f"Booking closed for {leg.seat_class} "
                    f"on flight {flight.flight_number}"
                )

            # Create one booking record for each leg.
            booking_id = conn.execute(text("""
                INSERT INTO bookings (
                    idempotency_key,
                    flight_id,
                    passenger_id,
                    seat_class,
                    fare,
                    price_paid,
                    currency,
                    status,
                    hold_expires_at,
                    group_id
                )
                VALUES (
                    :key,
                    :fid,
                    :pid,
                    :cls,
                    :fare,
                    :price,
                    'USD',
                    'held',
                    now() + interval '15 minutes',
                    :group_id
                )
                RETURNING id
            """), {
                "key": f"{idempotency_key}-leg-{index}",
                "fid": leg.flight_id,
                "pid": req.passenger_id,
                "cls": leg.seat_class,
                "fare": req.fare,
                "price": seat.base_price,
                "group_id": itinerary_id
            }).scalar()

            booking_ids.append(booking_id)
            total_price += float(seat.base_price)

        # ---------------------------------------------------------
        # Everything succeeded.
        # Transaction commits here.
        # ---------------------------------------------------------

    return {
        "itinerary_id": itinerary_id,
        "booking_ids": booking_ids,
        "legs": len(req.legs),
        "total_price": total_price,
        "status": "held",
        "hold_expires_in_minutes": HOLD_MINUTES,
        "message": (
            "All itinerary legs were successfully held atomically."
        )
    }

# ---------- Booking: confirm (payment succeeded) ----------

@app.post("/bookings/{booking_id}/confirm")
def confirm_booking(booking_id: int,
                    idempotency_key: str = Header(..., alias="Idempotency-Key")):
    with engine.begin() as conn:
        # IDEMPOTENCY: same confirmation key = never charge/confirm twice
        existing = conn.execute(text("""
            SELECT entity_id
            FROM audit_log
            WHERE action = 'confirm_booking'
              AND after_state->>'idempotency_key' = :key
        """), {"key": idempotency_key}).first()
        if existing:
            return {
                "booking_id": existing.entity_id,
                "status": "confirmed",
                "idempotent_replay": True,
                "message": "This confirmation request was already processed"
            }

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

        conn.execute(text("""
            INSERT INTO audit_log (actor, actor_role, action, entity_type, entity_id, after_state)
            VALUES ('customer', 'ops_agent', 'confirm_booking', 'booking', :bid, :after)
        """), {
            "bid": booking_id,
            "after": f'{{"status": "confirmed", "amount_paid": {float(row.price_paid)}, "idempotency_key": "{idempotency_key}"}}'
        })

        # Details needed for the confirmation email
        details = conn.execute(text("""
            SELECT p.full_name, p.email, f.flight_number, f.origin, f.destination,
                   f.departure_ts
            FROM bookings b
            JOIN passengers p ON p.id = b.passenger_id
            JOIN flights f ON f.id = b.flight_id
            WHERE b.id = :bid
        """), {"bid": booking_id}).first()

    if details:
        send_transactional_email(
            to_addr=details.email,
            subject=f"Booking Confirmed — Flight {details.flight_number}",
            body=(
                f"Dear {details.full_name},\n\n"
                f"Your booking is confirmed.\n\n"
                f"Booking ID: {booking_id}\n"
                f"Flight: {details.flight_number} ({details.origin} -> {details.destination})\n"
                f"Departure: {details.departure_ts}\n"
                f"Amount paid: {float(row.price_paid)}\n\n"
                f"Flight Management Team"
            ),
        )

    return {"booking_id": booking_id, "status": "confirmed",
            "amount_paid": float(row.price_paid)}


# ---------- Booking: group (all-or-nothing, idempotent via header) ----------

class GroupBookingCreate(BaseModel):
    flight_id: int
    passenger_ids: list[int]      # one seat per passenger
    seat_class: str
    fare: str


@app.post("/bookings/group", status_code=201)
def create_group_booking(req: GroupBookingCreate,
                         idempotency_key: str = Header(..., alias="Idempotency-Key")):
    n = len(req.passenger_ids)
    if n < 1:
        raise HTTPException(422, "At least one passenger required")

    with engine.begin() as conn:
        # Idempotency for the whole group
        existing = conn.execute(text("""
            SELECT group_id FROM bookings WHERE idempotency_key = :key
        """), {"key": idempotency_key + "-p0"}).first()
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
            RETURNING base_price, currency
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
                VALUES (:key, :fid, :pid, :cls, :fare, :price, :cur,
                        'held', now() + interval '15 minutes', :gid)
                RETURNING id
            """), {"key": f"{idempotency_key}-p{i}", "fid": req.flight_id,
                   "pid": pid, "cls": req.seat_class, "fare": req.fare,
                   "price": seat.base_price, "cur": str(seat.currency).strip(),
                   "gid": group_id}).scalar()
            booking_ids.append(bid)

    return {"group_id": group_id, "booking_ids": booking_ids,
            "seats_held": n, "price_each": float(seat.base_price),
            "hold_expires_in_minutes": HOLD_MINUTES}


# ---------- Booking: cancel (fare-type branching) ----------

@app.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: int,
                   idempotency_key: str = Header(..., alias="Idempotency-Key")):
    with engine.begin() as conn:
        # IDEMPOTENCY: same cancellation key = do not cancel/refund twice
        existing = conn.execute(text("""
            SELECT entity_id
            FROM audit_log
            WHERE action = 'cancel_booking'
              AND after_state->>'idempotency_key' = :key
        """), {"key": idempotency_key}).first()
        if existing:
            return {
                "booking_id": existing.entity_id,
                "status": "cancelled",
                "idempotent_replay": True,
                "message": "This cancellation request was already processed"
            }

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
               "after": f'{{"status": "cancelled", "refund": {refund_amount}, "refund_id": {refund_id if refund_id is not None else "null"}, "reason": "{refund_reason}", "idempotency_key": "{idempotency_key}"}}'})

        # Details needed for the cancellation receipt email
        details = conn.execute(text("""
            SELECT p.full_name, p.email, f.flight_number
            FROM bookings b
            JOIN passengers p ON p.id = b.passenger_id
            JOIN flights f ON f.id = b.flight_id
            WHERE b.id = :bid
        """), {"bid": booking_id}).first()

    if details:
        send_transactional_email(
            to_addr=details.email,
            subject=f"Cancellation Receipt — Flight {details.flight_number}",
            body=(
                f"Dear {details.full_name},\n\n"
                f"Your booking has been cancelled.\n\n"
                f"Booking ID: {booking_id}\n"
                f"Flight: {details.flight_number}\n"
                f"Fare type: {fare}\n"
                f"Refund amount: {refund_amount} ({refund_reason})\n\n"
                f"Flight Management Team"
            ),
        )

    return {"booking_id": booking_id, "status": "cancelled",
            "fare": fare, "hours_before_departure": round(float(hours_left), 1),
            "refund_amount": refund_amount, "refund_id": refund_id,
            "reason": refund_reason,
            "seat_released": True}


# ---------- Booking: partial cancellation inside a group ----------

class PartialCancel(BaseModel):
    booking_ids: list[int]     # which passengers' bookings to cancel


def compute_refund(fare: str, was_paid: bool, hours_left: float, price_paid: float):
    """Same fare-type rules as cancel_booking, reusable per booking."""
    if not was_paid:
        return 0, "no payment taken (hold released)"
    if fare == 'flexible':
        if hours_left > 24:
            return float(price_paid), "flexible fare, >24h before departure: full refund"
        return 0, "flexible fare, <24h before departure: no refund"
    if fare == 'basic_economy':
        return 0, "basic economy: non-refundable"
    raise HTTPException(422, f"No cancellation rule for fare '{fare}'")


@app.post("/bookings/group/{group_id}/cancel-partial")
def cancel_group_partial(group_id: str, req: PartialCancel,
                         idempotency_key: str = Header(..., alias="Idempotency-Key")):
    ids = list(set(req.booking_ids))
    if not ids:
        raise HTTPException(422, "Provide at least one booking_id")

    with engine.begin() as conn:
        # IDEMPOTENCY: same key = do not cancel the same batch twice
        existing = conn.execute(text("""
            SELECT after_state FROM audit_log
            WHERE action = 'cancel_group_partial'
              AND after_state->>'idempotency_key' = :key
        """), {"key": idempotency_key}).first()
        if existing:
            return {"group_id": group_id, "idempotent_replay": True,
                    "message": "This partial-cancellation request was already processed"}

        rows = conn.execute(text("""
            SELECT b.id, b.status, b.fare, b.seat_class, b.price_paid,
                   b.flight_id, f.departure_ts
            FROM bookings b
            JOIN flights f ON f.id = b.flight_id
            WHERE b.group_id = :gid AND b.id = ANY(:ids)
            FOR UPDATE OF b
        """), {"gid": group_id, "ids": ids}).fetchall()

        if len(rows) != len(ids):
            raise HTTPException(404,
                "One or more booking_ids do not belong to this group")

        hours_left = float(conn.execute(text("""
            SELECT EXTRACT(EPOCH FROM (:dep)::timestamptz - now()) / 3600
        """), {"dep": rows[0].departure_ts}).scalar())

        results = []
        total_refund = 0.0
        released = 0

        for b in rows:
            if b.status not in ('held', 'confirmed'):
                raise HTTPException(409,
                    f"Booking {b.id} is '{b.status}' and cannot be cancelled")

            refund_amount, reason = compute_refund(
                str(b.fare), b.status == 'confirmed', hours_left, b.price_paid)

            conn.execute(text("""
                UPDATE bookings SET status = 'cancelled', updated_at = now()
                WHERE id = :bid
            """), {"bid": b.id})

            refund_id = None
            if refund_amount > 0:
                refund_id = conn.execute(text("""
                    INSERT INTO refunds (booking_id, amount, status, created_at)
                    VALUES (:bid, :amt, 'pending', now())
                    RETURNING id
                """), {"bid": b.id, "amt": refund_amount}).scalar()

            total_refund += refund_amount
            released += 1
            results.append({"booking_id": b.id, "refund_amount": refund_amount,
                            "refund_id": refund_id, "reason": reason})

        # Release all freed seats in one atomic update (all bookings share flight+class)
        conn.execute(text("""
            UPDATE seat_classes SET available_seats = available_seats + :n
            WHERE flight_id = :fid AND class = :cls
        """), {"n": released, "fid": rows[0].flight_id, "cls": rows[0].seat_class})

        remaining = conn.execute(text("""
            SELECT count(*) FROM bookings
            WHERE group_id = :gid AND status IN ('held', 'confirmed')
        """), {"gid": group_id}).scalar()

        conn.execute(text("""
            INSERT INTO audit_log (actor, actor_role, action, entity_type,
                                   entity_id, before_state, after_state)
            VALUES ('customer', 'ops_agent', 'cancel_group_partial', 'booking',
                    :bid, :before, :after)
        """), {"bid": rows[0].id,
               "before": f'{{"group_id": "{group_id}", "booking_ids": {ids}}}',
               "after": f'{{"cancelled_count": {released}, "total_refund": {total_refund}, "idempotency_key": "{idempotency_key}"}}'})

    return {"group_id": group_id, "cancelled": results,
            "seats_released": released,
            "total_refund": total_refund,
            "remaining_active_bookings": remaining}


# ---------- Public: search available seats ----------
@app.get("/flights/upcoming")
def list_upcoming_flights():
    """Public listing of scheduled future flights — lets a browsing website
    show what's available without the caller already knowing a route/date."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT id, flight_number, origin, destination, departure_ts
            FROM flights
            WHERE status = 'scheduled' AND departure_ts > now()
            ORDER BY departure_ts
            LIMIT 50
        """)).fetchall()
    return {
        "flights": [
            {"flight_id": r.id, "flight_number": r.flight_number,
             "origin": r.origin, "destination": r.destination,
             "departure_ts": str(r.departure_ts)}
            for r in rows
        ]
    }

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
def join_waitlist(req: WaitlistJoin,
                  idempotency_key: str = Header(..., alias="Idempotency-Key")):
    with engine.begin() as conn:
        # IDEMPOTENCY: same waitlist key = do not create a second waitlist entry
        existing = conn.execute(text("""
            SELECT entity_id
            FROM audit_log
            WHERE action = 'join_waitlist'
              AND after_state->>'idempotency_key' = :key
        """), {"key": idempotency_key}).first()
        if existing:
            return {
                "waitlist_id": existing.entity_id,
                "idempotent_replay": True,
                "message": "This waitlist request was already processed"
            }

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

        conn.execute(text("""
            INSERT INTO audit_log (actor, actor_role, action, entity_type, entity_id, after_state)
            VALUES ('customer', 'ops_agent', 'join_waitlist', 'waitlist', :wid, :after)
        """), {
            "wid": wl_id,
            "after": f'{{"flight_id": {req.flight_id}, "passenger_id": {req.passenger_id}, "seat_class": "{req.seat_class}", "position": {position}, "idempotency_key": "{idempotency_key}"}}'
        })

    return {"waitlist_id": wl_id, "position": position,
            "message": f"Added to {req.seat_class} waitlist at position {position}"}


# ---------- Public: seat map for a flight ----------

@app.get("/flights/{flight_id}/seatmap")
def get_seatmap(flight_id: int):
    with engine.connect() as conn:
        flight = conn.execute(text("""
            SELECT id, flight_number FROM flights WHERE id = :fid
        """), {"fid": flight_id}).first()
        if not flight:
            raise HTTPException(404, "Flight not found")

        rows = conn.execute(text("""
            SELECT seat_number, class, is_occupied
            FROM seats
            WHERE flight_id = :fid
            ORDER BY class, seat_number
        """), {"fid": flight_id}).fetchall()

    return {
        "flight_id": flight_id,
        "flight_number": flight.flight_number,
        "seats": [
            {"seat_number": r.seat_number, "class": str(r.class_) if hasattr(r, 'class_') else r[1],
             "is_occupied": r.is_occupied}
            for r in rows
        ],
        "total_seats": len(rows)
    }