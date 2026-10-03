-- ============================================================
-- Flight Management Capstone — Database Schema
-- Reverse-documented from live Supabase Postgres via
-- information_schema.columns. This documents the actual
-- deployed schema; it does not need to be re-run (tables
-- already exist and contain data). Use it as a reference for
-- CHECK/FK/enum invariants, and as a base for a fresh
-- environment if ever needed.
-- ============================================================

-- ---------- Enum types (USER-DEFINED columns below reference these) ----------
-- Exact enum labels inferred from defaults seen in information_schema
-- (e.g. 'held'::booking_status). Confirm/extend labels against actual
-- CHECK/enum definitions in Supabase if any admin action rejects a value.

CREATE TYPE booking_status AS ENUM ('held', 'confirmed', 'cancelled', 'expired');
CREATE TYPE flight_status AS ENUM ('scheduled', 'cancelled');
CREATE TYPE seat_class_type AS ENUM ('first', 'business', 'economy');
CREATE TYPE refund_status AS ENUM ('pending', 'approved', 'processed', 'rejected', 'escalated');
CREATE TYPE admin_role AS ENUM ('super_admin', 'ops_agent');
-- fare (bookings.fare), class (seats.class) reuse seat_class_type /
-- a fare-specific enum — confirm exact type names via:
--   SELECT udt_name FROM information_schema.columns
--   WHERE table_name = 'bookings' AND column_name = 'fare';


-- ---------- flights ----------
CREATE TABLE flights (
    id                BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    flight_number     TEXT NOT NULL,
    origin            TEXT NOT NULL,
    destination       TEXT NOT NULL,
    origin_tz         TEXT NOT NULL,
    destination_tz    TEXT NOT NULL,
    departure_ts      TIMESTAMPTZ NOT NULL,
    arrival_ts        TIMESTAMPTZ NOT NULL,
    total_capacity    INTEGER NOT NULL,
    status            flight_status NOT NULL DEFAULT 'scheduled',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_flights_capacity_positive CHECK (total_capacity > 0),
    CONSTRAINT chk_flights_arrival_after_departure CHECK (arrival_ts > departure_ts),
    CONSTRAINT uq_flights_number_date UNIQUE (flight_number, (departure_ts::date))
);


-- ---------- seat_classes ----------
CREATE TABLE seat_classes (
    id                     BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    flight_id              BIGINT NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
    class                  seat_class_type NOT NULL,
    total_seats            INTEGER NOT NULL,
    available_seats        INTEGER NOT NULL,
    base_price             NUMERIC NOT NULL,
    currency               CHAR(3) NOT NULL DEFAULT 'USD',
    booking_cutoff_minutes INTEGER NOT NULL DEFAULT 60,

    CONSTRAINT chk_seat_classes_total_positive CHECK (total_seats > 0),
    CONSTRAINT chk_seat_classes_price_positive CHECK (base_price > 0),
    CONSTRAINT chk_seat_classes_available_range CHECK (available_seats >= 0 AND available_seats <= total_seats),
    CONSTRAINT uq_seat_classes_flight_class UNIQUE (flight_id, class)
);


-- ---------- seats (physical seat map) ----------
CREATE TABLE seats (
    id            BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    flight_id     BIGINT NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
    seat_number   TEXT NOT NULL,
    class         seat_class_type NOT NULL,
    is_occupied   BOOLEAN NOT NULL DEFAULT false,

    CONSTRAINT uq_seats_flight_seatnumber UNIQUE (flight_id, seat_number)
);


-- ---------- passengers ----------
CREATE TABLE passengers (
    id            BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    full_name     TEXT NOT NULL,
    email         TEXT NOT NULL,
    loyalty_tier  INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_passengers_loyalty_nonneg CHECK (loyalty_tier >= 0),
    CONSTRAINT uq_passengers_email UNIQUE (email)
);


-- ---------- bookings ----------
CREATE TABLE bookings (
    id                BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    idempotency_key   TEXT NOT NULL,
    flight_id         BIGINT NOT NULL REFERENCES flights(id),
    passenger_id      BIGINT NOT NULL REFERENCES passengers(id),
    seat_class        seat_class_type NOT NULL,
    seat_id           BIGINT REFERENCES seats(id),
    fare              TEXT NOT NULL,        -- USER-DEFINED enum; confirm exact type name
    price_paid        NUMERIC NOT NULL,
    currency          CHAR(3) NOT NULL DEFAULT 'USD',
    status            booking_status NOT NULL DEFAULT 'held',
    hold_expires_at   TIMESTAMPTZ,
    group_id          UUID,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    reminder_sent     BOOLEAN NOT NULL DEFAULT false,
    fraud_checked     BOOLEAN NOT NULL DEFAULT false,

    CONSTRAINT chk_bookings_price_positive CHECK (price_paid >= 0),
    CONSTRAINT uq_bookings_idempotency_key UNIQUE (idempotency_key)
);

CREATE INDEX idx_bookings_flight_id ON bookings(flight_id);
CREATE INDEX idx_bookings_passenger_id ON bookings(passenger_id);
CREATE INDEX idx_bookings_status ON bookings(status);
CREATE INDEX idx_bookings_group_id ON bookings(group_id);


-- ---------- payments ----------
CREATE TABLE payments (
    id            BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    booking_id    BIGINT NOT NULL REFERENCES bookings(id),
    amount        NUMERIC NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'USD',
    status        TEXT NOT NULL DEFAULT 'pending',
    paid_at       TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT now(),

    CONSTRAINT chk_payments_amount_positive CHECK (amount > 0),
    CONSTRAINT chk_payments_status_valid CHECK (status IN ('pending', 'completed', 'failed', 'refunded'))
);

CREATE INDEX idx_payments_booking_id ON payments(booking_id);


-- ---------- refunds ----------
CREATE TABLE refunds (
    id                  BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    booking_id          BIGINT NOT NULL REFERENCES bookings(id),
    amount              NUMERIC NOT NULL,
    status              refund_status NOT NULL DEFAULT 'pending',
    is_credit           BOOLEAN NOT NULL DEFAULT false,
    credit_expires_at   TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ,
    escalated           BOOLEAN NOT NULL DEFAULT false,
    escalated_at        TIMESTAMPTZ,

    CONSTRAINT chk_refunds_amount_nonneg CHECK (amount >= 0)
);

CREATE INDEX idx_refunds_booking_id ON refunds(booking_id);
CREATE INDEX idx_refunds_status ON refunds(status);


-- ---------- waitlist ----------
CREATE TYPE waitlist_status AS ENUM ('waiting', 'offered', 'promoted', 'expired', 'cancelled');

CREATE TABLE waitlist (
    id                BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    flight_id         BIGINT NOT NULL REFERENCES flights(id),
    passenger_id      BIGINT NOT NULL REFERENCES passengers(id),
    seat_class        seat_class_type NOT NULL,
    status            waitlist_status NOT NULL DEFAULT 'waiting',
    joined_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    offered_at        TIMESTAMPTZ,
    offer_expires_at  TIMESTAMPTZ,

    CONSTRAINT chk_waitlist_offer_expiry_after_offer CHECK (offer_expires_at IS NULL OR offered_at IS NULL OR offer_expires_at > offered_at)
);

CREATE INDEX idx_waitlist_flight_class_status ON waitlist(flight_id, seat_class, status);


-- ---------- support_queries ----------
CREATE TABLE support_queries (
    id                 BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    booking_id         BIGINT REFERENCES bookings(id),
    passenger_email    TEXT NOT NULL,
    question           TEXT NOT NULL,
    draft_answer       TEXT,
    approved           BOOLEAN,
    approved_by        TEXT,
    sent_at            TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------- fraud_flags ----------
CREATE TABLE fraud_flags (
    id            BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    booking_id    BIGINT NOT NULL REFERENCES bookings(id),
    score         NUMERIC NOT NULL,
    reasons       JSONB NOT NULL DEFAULT '[]',
    flagged_by    TEXT NOT NULL DEFAULT 'n8n_fraud_job',
    reviewed      BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_fraud_flags_score_range CHECK (score >= 0 AND score <= 100)
);

CREATE INDEX idx_fraud_flags_reviewed ON fraud_flags(reviewed);


-- ---------- notifications_log ----------
CREATE TABLE notifications_log (
    id                  BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    booking_id          BIGINT REFERENCES bookings(id),
    passenger_id        BIGINT REFERENCES passengers(id),
    notification_type   TEXT NOT NULL,
    sent_via            TEXT DEFAULT 'gmail',
    sent_at             TIMESTAMPTZ DEFAULT now(),
    details             JSONB
);

CREATE INDEX idx_notifications_log_booking_id ON notifications_log(booking_id);


-- ---------- policy_docs (RAG source docs for Pinecone ingestion) ----------
CREATE TABLE policy_docs (
    id            BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    title         TEXT NOT NULL,
    content       TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedded      BOOLEAN NOT NULL DEFAULT false,
    embedded_at   TIMESTAMPTZ
);


-- ---------- price_alerts ----------
CREATE TABLE price_alerts (
    id                  BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    passenger_id        BIGINT NOT NULL REFERENCES passengers(id),
    origin              TEXT NOT NULL,
    destination         TEXT NOT NULL,
    seat_class          seat_class_type NOT NULL DEFAULT 'economy',
    threshold_price     NUMERIC,
    last_alerted_price  NUMERIC,
    last_alerted_at     TIMESTAMPTZ,
    active              BOOLEAN NOT NULL DEFAULT true
);


-- ---------- audit_log ----------
CREATE TABLE audit_log (
    id             BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    actor          TEXT NOT NULL,
    actor_role     admin_role,
    action         TEXT NOT NULL,
    entity_type    TEXT NOT NULL,
    entity_id      BIGINT NOT NULL,
    before_state   JSONB,
    after_state    JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_log_entity ON audit_log(entity_type, entity_id);
CREATE INDEX idx_audit_log_created_at ON audit_log(created_at);


-- ============================================================
-- TABLE-OWNERSHIP RULES (per COORDINATION.md / feature list)
-- ============================================================
-- FastAPI is the SOLE WRITER of: flights, seat_classes, seats,
--   bookings, payments, refunds (except refund status updates
--   from Gmail-approval escalation, which is n8n)
-- n8n is the SOLE WRITER of: fraud_flags, notifications_log,
--   policy_docs, price_alerts, support_queries
-- SHARED writers (both systems, different columns/rows):
--   waitlist (FastAPI inserts on join; n8n updates status on
--     promotion — row-locked via FOR UPDATE SKIP LOCKED)
--   refunds (FastAPI creates; n8n escalates unresolved ones)
--   audit_log (both systems insert their own actions)
-- ============================================================